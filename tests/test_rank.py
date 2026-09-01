"""Live ranking: rank/pool_size computed against the in-play pool."""

import db


def _mk(name, status="new", score=None, verdict="", role="R"):
    aid = db.create_applicant(name=name, role_applied=role, source="seek")
    if status != "new":
        db.update_applicant(aid, status=status)
    if score is not None:
        db.set_screening(aid, {"verdict": verdict, "score": score, "summary": "s"})
    return aid


def test_rank_basic_ordering():
    a = _mk("Alice", score=80, verdict="hire_priority")
    b = _mk("Bob", score=60, verdict="consider")
    c = _mk("Carol", score=90, verdict="hire_priority")
    assert db.rank_info(a) == {"rank": 2, "pool_size": 3}
    assert db.rank_info(b) == {"rank": 3, "pool_size": 3}
    assert db.rank_info(c) == {"rank": 1, "pool_size": 3}


def test_rank_excludes_hired_rejected_and_unscored():
    a = _mk("Alice", score=80, verdict="hire_priority")
    _mk("Hired", status="hired", score=95, verdict="hire_priority")
    _mk("Rej", status="rejected", score=95, verdict="hire_priority")
    _mk("NoScore", status="new")
    assert db.rank_info(a) == {"rank": 1, "pool_size": 1}


def test_rank_tie_broken_by_verdict_strength():
    a = _mk("Alice", score=70, verdict="hire_priority")
    b = _mk("Bob", score=70, verdict="consider")
    c = _mk("Carl", score=70, verdict="likely_fake")
    assert db.rank_info(a)["rank"] == 1
    assert db.rank_info(b)["rank"] == 2
    assert db.rank_info(c)["rank"] == 3


def test_out_of_play_applicant_has_no_rank():
    h = _mk("Hired", status="hired", score=95, verdict="hire_priority")
    info = db.rank_info(h)
    assert info["rank"] is None
    assert info["pool_size"] == 0


def test_list_and_get_carry_rank():
    a = _mk("Alice", score=80, verdict="hire_priority")
    b = _mk("Bob", score=60, verdict="consider")
    lst = db.list_applicants()
    by_id = {d["id"]: d for d in lst}
    assert by_id[a]["rank"] == 1 and by_id[a]["pool_size"] == 2
    assert by_id[b]["rank"] == 2 and by_id[b]["pool_size"] == 2
    d = db.get_applicant(a)
    assert d["rank"] == 1 and d["pool_size"] == 2


def test_rank_survives_status_filter():
    # rank is computed against the FULL pool even when the list is filtered
    a = _mk("Alice", score=80, verdict="hire_priority")
    b = _mk("Bob", status="pool", score=60, verdict="consider")
    filtered = db.list_applicants(status="new")
    assert len(filtered) == 1
    assert filtered[0]["id"] == a
    assert filtered[0]["rank"] == 1
    assert filtered[0]["pool_size"] == 2
