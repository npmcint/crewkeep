"""CrewKeep — FastAPI app: auth, applicants pipeline + screening, staff retention.

Run:  uvicorn app:app --host 0.0.0.0 --port 8091   (or ./serve.sh)
Auth: cookie sessions (HttpOnly). Fail-closed: every /api/* route except
login/register requires a valid session. First user = admin bootstrap.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from fastapi import (Cookie, FastAPI, File, Form, HTTPException, Request,
                     Response, UploadFile)
from fastapi.responses import FileResponse, JSONResponse

import auth
import db
import feedback as feedback_mod
import llm as llm_mod
import resume as resume_mod
import screening as screening_mod

HOST = os.environ.get("CREWKEEP_HOST", "0.0.0.0")
PORT = int(os.environ.get("CREWKEEP_PORT", "8091"))
DATA_DIR = Path(os.environ.get("CREWKEEP_DATA", str(Path(__file__).parent / "data")))
RESUME_DIR = DATA_DIR / "resumes"
WEB_DIR = Path(__file__).parent / "web"

app = FastAPI(title="CrewKeep", docs_url=None, redoc_url=None)


def _resume_dir() -> Path:
    return Path(os.environ.get("CREWKEEP_DATA",
                               str(Path(__file__).parent / "data"))) / "resumes"


# --------------------------------------------------------------------------
# Auth middleware (fail-closed)
# --------------------------------------------------------------------------
PUBLIC_PATHS = {"/api/auth/login", "/api/auth/register", "/", "/index.html",
                "/favicon.ico"}


@app.middleware("http")
async def session_auth(request: Request, call_next):
    path = request.url.path
    if path.startswith("/static") or path in PUBLIC_PATHS:
        return await call_next(request)
    token = request.cookies.get(auth.COOKIE_NAME)
    user = auth.user_for_token(token or "")
    if user is None:
        return JSONResponse({"detail": "not authenticated"}, status_code=401)
    request.state.user = user
    return await call_next(request)


def current_user(request: Request) -> dict:
    return getattr(request.state, "user", None)


def _require_admin(request: Request) -> dict:
    u = current_user(request)
    if not u or not u["is_admin"]:
        raise HTTPException(status_code=403, detail="admin only")
    return u


# --------------------------------------------------------------------------
# Auth routes
# --------------------------------------------------------------------------
@app.post("/api/auth/register")
def register(body: dict, response: Response):
    if auth.user_count() > 0:
        raise HTTPException(status_code=403,
                            detail="registration closed — an admin already exists")
    try:
        u = auth.create_user(body.get("username", ""), body.get("password", ""),
                             body.get("display_name", ""), is_admin=True)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return login({"username": u["username"], "password": body["password"]}, response)


@app.post("/api/auth/login")
def login(body: dict, response: Response):
    u = auth.authenticate(body.get("username", ""), body.get("password", ""))
    if not u:
        raise HTTPException(status_code=401, detail="invalid username or password")
    token = auth.create_session(u["username"])
    response.set_cookie(auth.COOKIE_NAME, token, max_age=14 * 24 * 3600,
                        httponly=True, samesite="lax", path="/")
    return {"user": u}


@app.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
def me(request: Request):
    u = current_user(request)
    return {"user": u}


@app.post("/api/auth/password")
def change_password(body: dict, request: Request):
    u = current_user(request)
    if not auth.authenticate(u["username"], body.get("old_password", "")):
        raise HTTPException(status_code=400, detail="current password incorrect")
    try:
        auth.set_password(u["username"], body.get("new_password", ""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@app.post("/api/auth/users")
def admin_add_user(body: dict, request: Request):
    _require_admin(request)
    try:
        u = auth.create_user(body.get("username", ""), body.get("password", ""),
                             body.get("display_name", ""),
                             is_admin=bool(body.get("is_admin", False)))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return u


@app.get("/api/auth/users")
def admin_list_users(request: Request):
    _require_admin(request)
    return auth.list_users()


@app.delete("/api/auth/users/{username}")
def admin_delete_user(username: str, request: Request):
    admin = _require_admin(request)
    if username == admin["username"]:
        raise HTTPException(status_code=400, detail="cannot delete yourself")
    auth.delete_user(username)
    return {"ok": True}


# --------------------------------------------------------------------------
# LLM engine status
# --------------------------------------------------------------------------
@app.get("/api/llm/test")
def llm_test():
    return llm_mod.test_connection()


# --------------------------------------------------------------------------
# Applicants
# --------------------------------------------------------------------------
@app.get("/api/applicants")
def applicants(status: str | None = None, q: str | None = None):
    return db.list_applicants(status=status, q=q)


@app.post("/api/applicants")
async def add_applicant(request: Request, name: str = Form(""),
                        phone: str = Form(""), email: str = Form(""),
                        source: str = Form("other"),
                        role_applied: str = Form(""),
                        resume: UploadFile | None = File(None)):
    resume_text = ""
    resume_path = ""
    if resume is not None and resume.filename:
        ext = Path(resume.filename).suffix.lower()
        if ext not in (".pdf", ".docx", ".doc", ".txt", ".md"):
            raise HTTPException(status_code=400,
                                detail="resume must be pdf/docx/txt")
        RESUME_DIR = _resume_dir()
        RESUME_DIR.mkdir(parents=True, exist_ok=True)
        safe = "".join(c for c in (name.strip() or "resume") if c.isalnum() or c in " _-")[:40]
        dest = RESUME_DIR / f"{safe}_{int(__import__('time').time())}{ext}"
        with dest.open("wb") as f:
            shutil.copyfileobj(resume.file, f)
        try:
            resume_text = resume_mod.parse_resume(dest)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"could not read resume: {e}")
        resume_path = str(dest)
    if not name.strip() and resume_text.strip():
        # batch import: derive the applicant's name from the CV itself
        name = resume_mod.guess_name(resume_text, resume.filename if resume else "")
    if not name.strip():
        raise HTTPException(status_code=400,
                            detail="name is required (couldn't guess it from the resume)")
    try:
        aid = db.create_applicant(name=name, phone=phone, email=email,
                                  source=source, role_applied=role_applied,
                                  resume_path=resume_path, resume_text=resume_text)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.ensure_checks(aid)
    return db.get_applicant(aid)

@app.get("/api/applicants/{aid}")
def applicant(aid: int):
    d = db.get_applicant(aid)
    if not d:
        raise HTTPException(status_code=404, detail="not found")
    return d


@app.patch("/api/applicants/{aid}")
def patch_applicant(aid: int, body: dict):
    try:
        d = db.update_applicant(aid, **body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not d:
        raise HTTPException(status_code=404, detail="not found")
    return d


@app.delete("/api/applicants/{aid}")
def del_applicant(aid: int):
    db.delete_applicant(aid)
    return {"ok": True}


@app.post("/api/applicants/{aid}/screen")
def run_screen(aid: int):
    d = db.get_applicant(aid)
    if not d:
        raise HTTPException(status_code=404, detail="not found")
    text = d.get("resume_text") or ""
    if not text.strip():
        raise HTTPException(status_code=400,
                            detail="no resume text to screen — add a resume first")
    # Pool context = every OTHER in-play applicant with a score, so the
    # screening can rank this candidate against the field (boss_context).
    pool = []
    for other in db.list_applicants():
        if other["id"] == aid or other["status"] not in db.IN_PLAY_STATUSES:
            continue
        if not (other.get("score") or 0) > 0:
            continue
        scr = other.get("screening") or {}
        pool.append({"name": other["name"],
                     "role_applied": other.get("role_applied", ""),
                     "score": other.get("score"),
                     "verdict": other.get("verdict", ""),
                     "summary": scr.get("summary", "")})
    report = screening_mod.screen(d["name"], d.get("role_applied", ""), text,
                                  pool=pool)
    db.set_screening(aid, report)
    return db.get_applicant(aid)


@app.get("/api/applicants/{aid}/resume")
def download_resume(aid: int):
    d = db.get_applicant(aid)
    if not d or not d.get("resume_path"):
        raise HTTPException(status_code=404, detail="no resume on file")
    p = Path(d["resume_path"])
    if not p.exists():
        raise HTTPException(status_code=404, detail="resume file missing")
    return FileResponse(p, filename=p.name)


@app.post("/api/applicants/{aid}/notes")
def add_note(aid: int, body: dict, request: Request):
    note = (body.get("note") or "").strip()
    if not note:
        raise HTTPException(status_code=400, detail="note is empty")
    u = current_user(request)
    return db.add_applicant_note(aid, u["username"], note)


@app.get("/api/applicants/{aid}/checks")
def checks(aid: int):
    return db.ensure_checks(aid)


@app.post("/api/applicants/{aid}/checks")
def set_check(aid: int, body: dict):
    try:
        row = db.set_check(aid, body.get("check_name", ""),
                           bool(body.get("done", False)),
                           body.get("note", ""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if row is None:
        raise HTTPException(status_code=404, detail="check not found")
    return row


# --------------------------------------------------------------------------
# Feedback (in-app feature requests / bug reports — jobhunt pattern)
# --------------------------------------------------------------------------
import time as _time

_FB_SUBMISSIONS: dict[str, list[float]] = {}  # user -> recent submit timestamps


def _fb_rate_limited(username: str, max_n: int = 5, window_s: int = 900) -> bool:
    now = _time.time()
    recent = [t for t in _FB_SUBMISSIONS.get(username, []) if now - t < window_s]
    if len(recent) >= max_n:
        _FB_SUBMISSIONS[username] = recent
        return True
    recent.append(now)
    _FB_SUBMISSIONS[username] = recent
    return False


@app.post("/api/feedback")
def submit_feedback(body: dict, request: Request):
    u = current_user(request)
    if _fb_rate_limited(u["username"]):
        raise HTTPException(429, "too many submissions — try again later")
    try:
        item_id = feedback_mod.submit(u["username"], body.get("category", ""),
                                      body.get("message", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "id": item_id, "message": "thanks — recorded"}


@app.get("/api/feedback/mine")
def feedback_mine(request: Request):
    u = current_user(request)
    return {"items": feedback_mod.mine(u["username"])}


@app.get("/api/feedback")
def list_feedback(status: str | None = None, request: Request = None):
    _require_admin(request)
    return {"items": feedback_mod.list_items(status=status),
            "counts": feedback_mod.counts()}


@app.post("/api/feedback/{item_id}/status")
def set_feedback_status(item_id: int, body: dict, request: Request):
    _require_admin(request)
    try:
        item = feedback_mod.set_status(item_id, body.get("status", ""),
                                       body.get("note", ""))
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True, "item": item}


# --------------------------------------------------------------------------
# Staff (retention)
# --------------------------------------------------------------------------
@app.get("/api/staff")
def staff_list(status: str | None = None, q: str | None = None):
    return db.list_staff(status=status, q=q)


@app.post("/api/staff")
def add_staff(body: dict):
    try:
        sid = db.create_staff(name=body.get("name", ""), phone=body.get("phone", ""),
                              email=body.get("email", ""), role=body.get("role", ""),
                              start_date=body.get("start_date", ""),
                              rate=body.get("rate", ""),
                              supervisor=body.get("supervisor", ""),
                              site=body.get("site", ""),
                              skills=body.get("skills", ""),
                              notes=body.get("notes", ""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return db.get_staff(sid)


@app.get("/api/staff/{sid}")
def staff_detail(sid: int):
    d = db.get_staff(sid)
    if not d:
        raise HTTPException(status_code=404, detail="not found")
    return d


@app.patch("/api/staff/{sid}")
def patch_staff(sid: int, body: dict):
    d = db.update_staff(sid, **body)
    if not d:
        raise HTTPException(status_code=404, detail="not found")
    return d


@app.delete("/api/staff/{sid}")
def del_staff(sid: int):
    conn = db.connect()
    try:
        conn.execute("DELETE FROM staff WHERE id=?", (sid,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@app.post("/api/staff/{sid}/onboarding")
def onboarding(sid: int, body: dict):
    try:
        db.set_onboarding(sid, body.get("milestone", ""),
                          bool(body.get("done", False)), body.get("note", ""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return db.get_staff(sid)


@app.post("/api/staff/{sid}/stay")
def stay(sid: int, body: dict):
    try:
        row = db.add_stay_interview(sid, conducted_at=body.get("conducted_at", ""),
                                    q_keep=body.get("q_keep", ""),
                                    q_tempt=body.get("q_tempt", ""),
                                    q_better=body.get("q_better", ""),
                                    risk=body.get("risk", "low"),
                                    note=body.get("note", ""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return row


@app.post("/api/staff/{sid}/flags")
def flag(sid: int, body: dict):
    try:
        row = db.add_staff_flag(sid, body.get("kind", "other"),
                                body.get("note", ""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return row


@app.post("/api/flags/{fid}/resolve")
def resolve(fid: int):
    db.resolve_flag(fid)
    return {"ok": True}


@app.post("/api/staff/{sid}/exit")
def exit_staff(sid: int, body: dict):
    try:
        row = db.record_exit(sid, exit_date=body.get("exit_date", ""),
                             reason=body.get("reason", "other"),
                             note=body.get("note", ""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return row


# --------------------------------------------------------------------------
# Dashboard + static
# --------------------------------------------------------------------------
@app.get("/api/dashboard")
def dashboard():
    return db.dashboard()


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


def main():
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
