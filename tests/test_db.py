import db


def _mk_applicant(**kw):
    args = dict(name=kw.pop("name", "Jim Roof"),
                phone=kw.pop("phone", "0400 000 000"),
                email=kw.pop("email", "jim@x.com"),
                source=kw.pop("source", "seek"),
                role_applied=kw.pop("role_applied", "Roof plumber"))
    return db.create_applicant(**args)


def test_applicant_crud():
    aid = _mk_applicant()
    d = db.get_applicant(aid)
    assert d["name"] == "Jim Roof"
    assert d["status"] == "new"
    assert d["checks"] == []  # checks only exist after ensure_checks
    # notes
    n = db.add_applicant_note(aid, "boss", "rang, sounded keen")
    assert n["note"] == "rang, sounded keen"
    assert len(db.get_applicant(aid)["notes"]) == 1
    # update
    d = db.update_applicant(aid, status="interview", notes="blurb")
    assert d["status"] == "interview"
    assert d["hired_at"] == "" or d["hired_at"] is None
    d = db.update_applicant(aid, status="hired")
    assert d["hired_at"]
    import pytest
    with pytest.raises(ValueError):
        db.update_applicant(aid, status="bogus")
    db.delete_applicant(aid)
    assert db.get_applicant(aid) is None


def test_blank_name_rejected():
    import pytest
    with pytest.raises(ValueError):
        db.create_applicant("   ")


def test_screening_persist():
    aid = _mk_applicant()
    db.set_screening(aid, {"verdict": "consider", "score": 62, "x": 1})
    d = db.get_applicant(aid)
    assert d["verdict"] == "consider"
    assert d["score"] == 62
    assert d["screening"]["x"] == 1


def test_checks():
    aid = _mk_applicant()
    checks = db.ensure_checks(aid)
    assert {c["check_name"] for c in checks} == set(db.CHECK_NAMES)
    row = db.set_check(aid, "white_card", True, "seen photo")
    assert row["done"] == 1
    d = db.get_applicant(aid)
    by_name = {c["check_name"]: c for c in d["checks"]}
    assert by_name["white_card"]["done"] == 1
    # idempotent ensure
    assert len(db.ensure_checks(aid)) == len(db.CHECK_NAMES)


def test_applicant_list_filters():
    a1 = _mk_applicant(name="Alice", status=None)
    a2 = _mk_applicant(name="Bob", status=None)
    db.update_applicant(a2, status="pool")
    assert {a["name"] for a in db.list_applicants(status="pool")} == {"Bob"}
    assert {a["name"] for a in db.list_applicants(q="ali")} == {"Alice"}
    assert len(db.list_applicants()) == 2


def test_staff_create_has_onboarding():
    sid = db.create_staff(name="Mick", role="Roof plumber",
                          start_date="2026-01-10", supervisor="Boss")
    d = db.get_staff(sid)
    assert len(d["onboarding"]) == 4
    assert {m["milestone"] for m in d["onboarding"]} == set(db.STAFF_MILESTONES)
    db.set_onboarding(sid, "week1", True, "settling in well")
    d = db.get_staff(sid)
    m = {x["milestone"]: x for x in d["onboarding"]}["week1"]
    assert m["done"] == 1 and m["done_at"]
    assert d["risk"] == "low"


def test_staff_retention_flow():
    sid = db.create_staff(name="Dave", role="Labourer")
    si = db.add_stay_interview(sid, "2026-08-01", q_keep="crew", q_tempt="more pay",
                               risk="high")
    assert si["risk"] == "high"
    d = db.get_staff(sid)
    assert d["risk"] == "high"
    assert len(d["stay_interviews"]) == 1
    # flags
    f = db.add_staff_flag(sid, "absence", "3 days off, no call")
    assert len(db.get_staff(sid)["flags"]) == 1
    db.resolve_flag(f["id"])
    assert db.get_staff(sid)["flags"][0]["resolved"] == 1
    # exit flips status and records reason
    ex = db.record_exit(sid, "2026-09-01", reason="management",
                        note="left for family business")
    assert ex["reason"] == "management"
    assert db.get_staff(sid)["status"] == "left"


def test_dashboard_aggregates():
    a1 = db.create_applicant("N1", source="seek", role_applied="R")
    a2 = db.create_applicant("N2", source="gumtree", role_applied="R")
    db.update_applicant(a1, status="pool")
    db.update_applicant(a2, status="hired")
    s1 = db.create_staff(name="S1", role="R")
    s2 = db.create_staff(name="S2", role="R")
    db.add_stay_interview(s1, "2026-08-01", risk="high")
    db.record_exit(s2, "2026-08-15", reason="pay")
    dash = db.dashboard()
    assert dash["headcount"] == {"active": 1, "left": 1}
    assert dash["applicants_by_status"]["pool"] == 1
    assert dash["applicants_by_status"]["hired"] == 1
    assert [a["name"] for a in dash["at_risk"]] == ["S1"]
    assert dash["open_flags"] == 0
    assert dash["exits_by_reason"] == {"pay": 1}
