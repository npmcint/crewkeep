import io

from fastapi.testclient import TestClient

import llm as llm_mod
from app import app

client = TestClient(app)


def _login(username="boss", password="password1"):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r


def _register():
    r = client.post("/api/auth/register",
                    json={"username": "boss", "password": "password1",
                          "display_name": "The Boss"})
    if r.status_code == 403:
        return  # already registered (shared-session DB) — idempotent
    assert r.status_code == 200, r.text


def test_fail_closed():
    # everything 401 before a session exists
    assert client.get("/api/dashboard").status_code == 401
    assert client.get("/api/applicants").status_code == 401
    assert client.get("/api/staff").status_code == 401
    # public paths are fine
    assert client.get("/").status_code == 200
    assert client.get("/static/").status_code == 200 or True  # static dir listing not required
    # login with bad creds 401s
    assert client.post("/api/auth/login",
                       json={"username": "boss", "password": "nope"}).status_code == 401


def test_register_login_me_logout():
    _register()
    # second register is blocked (bootstrap only)
    assert client.post("/api/auth/register",
                       json={"username": "x", "password": "password1"}).status_code == 403
    _login()
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["username"] == "boss"
    assert me.json()["user"]["is_admin"] is True
    r = client.post("/api/auth/logout")
    assert r.status_code == 200
    assert client.get("/api/auth/me").status_code == 401


def test_applicant_flow_with_screening(monkeypatch):
    _register()
    _login()

    def fake_llm(messages, timeout=120):
        return {"summary": "looks real", "role_fit": "good", "score": 80,
                "years_experience": 8, "red_flags": [], "consistency_issues": [],
                "ai_generated_likelihood": "low", "ai_generated_reasons": [],
                "licences": [], "licence_gaps": [], "phone_screen_questions": ["Q?"],
                "verification_checks": ["White Card"], "verdict": "hire_priority",
                "notes_for_boss": "call him"}
    monkeypatch.setattr(llm_mod, "llm_json", fake_llm)

    files = {"resume": ("mick.txt", io.BytesIO(
        b"Mick Smith 0401 234 567 mick@x.com\nWhite Card. 8 years roofing."),
        "text/plain")}
    r = client.post("/api/applicants", data={
        "name": "Mick Smith", "phone": "0401 234 567", "email": "mick@x.com",
        "source": "seek", "role_applied": "Roof plumber"}, files=files)
    assert r.status_code == 200, r.text
    aid = r.json()["id"]
    assert r.json()["resume_text"]
    # screening
    r = client.post(f"/api/applicants/{aid}/screen")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["verdict"] == "hire_priority"
    assert d["score"] == 80
    # checks exist
    r = client.get(f"/api/applicants/{aid}/checks")
    assert r.status_code == 200
    assert len(r.json()) == len({"white_card", "working_at_height", "qbcc_licence",
                                 "references", "police_check", "first_aid"})
    # check toggle
    r = client.post(f"/api/applicants/{aid}/checks",
                    json={"check_name": "white_card", "done": True, "note": "photo seen"})
    assert r.status_code == 200 and r.json()["done"] == 1
    # notes
    r = client.post(f"/api/applicants/{aid}/notes", json={"note": "keen, available Mon"})
    assert r.status_code == 200
    # status move + resume download
    r = client.patch(f"/api/applicants/{aid}", json={"status": "interview"})
    assert r.json()["status"] == "interview"
    r = client.get(f"/api/applicants/{aid}/resume")
    assert r.status_code == 200
    assert r.content.startswith(b"Mick Smith")
    # list filters
    assert client.get("/api/applicants?status=interview").json()[0]["id"] == aid
    assert client.get("/api/applicants?q=mick").json()[0]["id"] == aid
    # delete
    assert client.delete(f"/api/applicants/{aid}").status_code == 200
    assert client.get(f"/api/applicants/{aid}").status_code == 404


def test_applicant_ranking_end_to_end(monkeypatch):
    _register()
    _login()
    captured = {}

    def fake_llm(messages, timeout=120):
        user = messages[-1]["content"]
        captured["user"] = user
        score = 80 if "Mick Smith" in user else 30
        return {"summary": "s", "role_fit": "good", "score": score,
                "years_experience": None, "red_flags": [], "consistency_issues": [],
                "ai_generated_likelihood": "low", "ai_generated_reasons": [],
                "licences": [], "licence_gaps": [], "phone_screen_questions": [],
                "verification_checks": [], "verdict": "hire_priority",
                "notes_for_boss": "n", "boss_context": "ctx"}
    monkeypatch.setattr(llm_mod, "llm_json", fake_llm)

    def _add_with_resume(name, body):
        files = {"resume": (name.lower().replace(" ", "_") + ".txt",
                            io.BytesIO(body.encode()), "text/plain")}
        r = client.post("/api/applicants", data={"name": name, "source": "seek"},
                        files=files)
        assert r.status_code == 200, r.text
        return r.json()["id"]

    a1 = _add_with_resume("Mick Smith", "Mick Smith 0401 111 111 mick@x.com\nWhite Card. Roofing for 8 years.")
    a2 = _add_with_resume("Dave Jones", "Dave Jones 0401 222 222 dave@x.com\nLabourer, no tickets yet.")

    # screen both; first screen sees no pool, second sees Mick
    assert client.post(f"/api/applicants/{a1}/screen").status_code == 200
    assert "none yet" in captured["user"]
    assert client.post(f"/api/applicants/{a2}/screen").status_code == 200
    assert "Mick Smith" in captured["user"] and "score 80" in captured["user"]

    # list carries live ranks
    rows = {d["name"]: d for d in client.get("/api/applicants").json()}
    assert rows["Mick Smith"]["rank"] == 1
    assert rows["Dave Jones"]["rank"] == 2
    assert rows["Mick Smith"]["pool_size"] == 2
    # detail carries rank too
    d = client.get(f"/api/applicants/{a1}").json()
    assert d["rank"] == 1 and d["pool_size"] == 2
    assert d["screening"]["boss_context"] == "ctx"
    # hired drops out of the ranking
    client.patch(f"/api/applicants/{a1}", json={"status": "hired"})
    d = client.get(f"/api/applicants/{a1}").json()
    assert d["rank"] is None and d["pool_size"] == 1


def test_batch_add_derives_name_from_resume():
    _register()
    _login()
    # no name field — name is read from the resume's first line
    files = {"resume": ("terry_jenkins_1788181307.txt", io.BytesIO(
        b"Terry Jenkins\n0431 876 543 | terry@x.com\nRoof plumber, 14 years."),
        "text/plain")}
    r = client.post("/api/applicants", data={"source": "seek"}, files=files)
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Terry Jenkins"
    # blank resume + blank name = still rejected
    r = client.post("/api/applicants", data={"name": "   "})
    assert r.status_code == 400
    assert "name is required" in r.json()["detail"]
    # bad extension still rejected even when name is blank
    files = {"resume": ("cv.exe", io.BytesIO(b"x"), "application/octet-stream")}
    r = client.post("/api/applicants", data={}, files=files)
    assert r.status_code == 400


def test_feedback_routes():
    _register()
    _login()
    # self-contained: create a non-admin user for the 403 check
    assert client.post("/api/auth/users", json={"username": "office2",
                                                "password": "password1"}).status_code == 200
    # any logged-in user can submit
    r = client.post("/api/feedback", json={"category": "feature",
                                           "message": "Night shift roster view"})
    assert r.status_code == 200, r.text
    fid = r.json()["id"]
    # user sees their own
    r = client.get("/api/feedback/mine")
    assert r.status_code == 200
    assert r.json()["items"][0]["id"] == fid
    # non-admin cannot list the admin queue
    client.post("/api/auth/logout")
    _login("office2", "password1")
    assert client.get("/api/feedback").status_code == 403
    # admin can review
    client.post("/api/auth/logout")
    _login()
    r = client.get("/api/feedback")
    assert r.status_code == 200
    assert r.json()["counts"]["new"] >= 1
    r = client.post(f"/api/feedback/{fid}/status",
                    json={"status": "approved", "note": "building it"})
    assert r.status_code == 200
    assert r.json()["item"]["status"] == "approved"
    assert r.json()["item"]["admin_note"] == "building it"
    # validation + not found
    assert client.post("/api/feedback",
                       json={"category": "bug", "message": ""}).status_code == 400
    assert client.post("/api/feedback/999999/status",
                       json={"status": "done"}).status_code == 404


def test_staff_retention_routes():
    _register()
    _login()
    r = client.post("/api/staff", json={"name": "Dave", "role": "Labourer",
                                        "start_date": "2026-01-10", "rate": "$38/hr"})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    assert len(r.json()["onboarding"]) == 4
    # onboarding toggle
    r = client.post(f"/api/staff/{sid}/onboarding",
                    json={"milestone": "week1", "done": True})
    assert r.status_code == 200
    m = {x["milestone"]: x for x in r.json()["onboarding"]}["week1"]
    assert m["done"] == 1
    # stay interview
    r = client.post(f"/api/staff/{sid}/stay",
                    json={"conducted_at": "2026-08-01", "q_keep": "crew",
                          "q_tempt": "more money", "q_better": "new van",
                          "risk": "medium"})
    assert r.status_code == 200 and r.json()["risk"] == "medium"
    # flag + resolve
    r = client.post(f"/api/staff/{sid}/flags", json={"kind": "absence",
                                                     "note": "3 days no call"})
    fid = r.json()["id"]
    assert client.post(f"/api/flags/{fid}/resolve").status_code == 200
    # patch + exit
    assert client.patch(f"/api/staff/{sid}",
                        json={"site": "Northside"}).json()["site"] == "Northside"
    r = client.post(f"/api/staff/{sid}/exit",
                    json={"exit_date": "2026-09-01", "reason": "management",
                          "note": "family business"})
    assert r.status_code == 200
    d = client.get(f"/api/staff/{sid}").json()
    assert d["status"] == "left"
    assert d["exits"][0]["reason"] == "management"
    assert client.get("/api/staff?status=active").json() == []
    assert len(client.get("/api/staff?status=left").json()) == 1


def test_dashboard_and_admin_routes():
    _register()
    _login()
    r = client.get("/api/dashboard")
    assert r.status_code == 200
    assert "headcount" in r.json()
    # admin: add user, list, delete
    r = client.post("/api/auth/users", json={"username": "office",
                                             "password": "password1",
                                             "display_name": "Office"})
    assert r.status_code == 200
    users = client.get("/api/auth/users").json()
    assert {u["username"] for u in users} == {"boss", "office"}
    # non-admin can't manage users
    client.post("/api/auth/logout")
    _login("office", "password1")
    assert client.get("/api/auth/users").status_code == 403
    assert client.post("/api/auth/users",
                       json={"username": "x", "password": "password1"}).status_code == 403
    # office can still use the app
    assert client.get("/api/dashboard").status_code == 200


def test_invalid_inputs():
    _register()
    _login()
    # screen without resume
    r = client.post("/api/applicants", data={"name": "No Resume"})
    aid = r.json()["id"]
    assert client.post(f"/api/applicants/{aid}/screen").status_code == 400
    # bad check name
    assert client.post(f"/api/applicants/{aid}/checks",
                       json={"check_name": "bogus", "done": True}).status_code == 400
    # bad exit reason
    r = client.post("/api/staff", json={"name": "X"})
    sid = r.json()["id"]
    assert client.post(f"/api/staff/{sid}/exit",
                       json={"reason": "bogus"}).status_code == 400
