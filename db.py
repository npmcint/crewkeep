"""CrewKeep data layer — SQLite schema + CRUD.

Two domains:
- APPLICANTS: people who applied for a job (pipeline + screening + checks)
- STAFF: current/former employees (retention: onboarding, stay interviews,
  early-warning flags, exits)

Dates are ISO strings (UTC where generated, local calendar dates where entered).
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("CREWKEEP_DATA", str(Path(__file__).parent / "data")))
DB_PATH = DATA_DIR / "crewkeep.db"

# --------------------------------------------------------------------------
# Domain constants
# --------------------------------------------------------------------------
APPLICANT_STATUSES = ("new", "phone_screen", "interview", "trial",
                      "hired", "rejected", "pool")
# 'pool' = good but not now (pre-screened pipeline that kills panic hiring)
VERDICTS = ("hire_priority", "consider", "weak", "likely_fake")
SOURCES = ("seek", "gumtree", "facebook", "referral", "walk_in", "other")

# Statuses that still count as "in play" for the live ranking — hired/rejected
# drop out of the leaderboard (and therefore out of rank/pool_size).
IN_PLAY_STATUSES = ("new", "phone_screen", "interview", "trial", "pool")
# Tie-break order when scores are equal (stronger verdict ranks higher).
VERDICT_PRIORITY = {"hire_priority": 4, "consider": 3, "weak": 2, "likely_fake": 1}

CHECK_NAMES = ("white_card", "working_at_height", "qbcc_licence",
               "references", "police_check", "first_aid")

STAFF_MILESTONES = ("day1", "week1", "month1", "day90")
FLAG_KINDS = ("absence", "late", "complaint", "engagement", "other")
EXIT_REASONS = ("pay", "management", "progression", "hours", "conditions",
                "travelled_on", "other")
RISK_LEVELS = ("low", "medium", "high")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(r: sqlite3.Row | None) -> dict | None:
    return dict(r) if r is not None else None


def data_dir() -> Path:
    return Path(os.environ.get("CREWKEEP_DATA", str(Path(__file__).parent / "data")))


def connect() -> sqlite3.Connection:
    db_path = data_dir() / "crewkeep.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS applicants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        phone TEXT DEFAULT '',
        email TEXT DEFAULT '',
        source TEXT DEFAULT 'other',
        role_applied TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'new',
        verdict TEXT DEFAULT '',
        score INTEGER DEFAULT 0,
        resume_path TEXT DEFAULT '',
        resume_text TEXT DEFAULT '',
        screening TEXT DEFAULT '',       -- JSON screening report
        screening_ts TEXT DEFAULT '',
        notes TEXT DEFAULT '',           -- short free-text blurb
        created_at TEXT,
        updated_at TEXT,
        hired_at TEXT
    );
    CREATE TABLE IF NOT EXISTS applicant_notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        applicant_id INTEGER NOT NULL REFERENCES applicants(id) ON DELETE CASCADE,
        author TEXT DEFAULT '',
        note TEXT NOT NULL,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS applicant_checks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        applicant_id INTEGER NOT NULL REFERENCES applicants(id) ON DELETE CASCADE,
        check_name TEXT NOT NULL,
        done INTEGER DEFAULT 0,
        note TEXT DEFAULT '',
        updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS staff (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        phone TEXT DEFAULT '',
        email TEXT DEFAULT '',
        role TEXT DEFAULT '',
        start_date TEXT DEFAULT '',
        rate TEXT DEFAULT '',
        supervisor TEXT DEFAULT '',
        site TEXT DEFAULT '',
        skills TEXT DEFAULT '',
        notes TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active',   -- active | left
        created_at TEXT,
        updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS staff_onboarding (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        staff_id INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
        milestone TEXT NOT NULL,
        done INTEGER DEFAULT 0,
        done_at TEXT DEFAULT '',
        note TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS stay_interviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        staff_id INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
        conducted_at TEXT,
        q_keep TEXT DEFAULT '',
        q_tempt TEXT DEFAULT '',
        q_better TEXT DEFAULT '',
        risk TEXT DEFAULT 'low',
        note TEXT DEFAULT '',
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS staff_flags (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        staff_id INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        note TEXT NOT NULL,
        created_at TEXT,
        resolved INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS exits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        staff_id INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
        exit_date TEXT DEFAULT '',
        reason TEXT DEFAULT 'other',
        note TEXT DEFAULT '',
        created_at TEXT
    );
    """)
    conn.commit()
    return conn


def _json_loads(s: str) -> dict:
    try:
        return json.loads(s) if s else {}
    except Exception:
        return {}


# --------------------------------------------------------------------------
# Applicants
# --------------------------------------------------------------------------
def create_applicant(name: str, phone: str = "", email: str = "",
                     source: str = "other", role_applied: str = "",
                     resume_path: str = "", resume_text: str = "") -> int:
    name = name.strip()
    if not name:
        raise ValueError("name is required")
    now = _now()
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO applicants (name, phone, email, source, role_applied, "
            "resume_path, resume_text, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (name.strip(), phone.strip(), email.strip(), source,
             role_applied.strip(), resume_path, resume_text, now, now))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _ranked_pool() -> list[dict]:
    """All in-play applicants with a score, ordered by rank: score desc,
    verdict strength (tie-break), then earliest application first. Computed
    live on every read — never stored — so the leaderboard stays correct as
    scores, verdicts and statuses change."""
    conn = connect()
    try:
        rows = [_row(r) for r in conn.execute(
            "SELECT id, name, role_applied, status, verdict, score, created_at "
            "FROM applicants WHERE status IN (%s) AND score > 0"
            % ",".join("?" * len(IN_PLAY_STATUSES)), list(IN_PLAY_STATUSES))]
    finally:
        conn.close()
    rows.sort(key=lambda d: (-(d["score"] or 0),
                             -VERDICT_PRIORITY.get(d["verdict"] or "", 0),
                             d["created_at"] or ""))
    return rows


def rank_info(aid: int) -> dict:
    """Position of an applicant vs the in-play pool: {'rank': int|None,
    'pool_size': int}. rank is None when the applicant is out of play
    (hired/rejected) or has no score yet."""
    pool = _ranked_pool()
    for i, d in enumerate(pool):
        if d["id"] == aid:
            return {"rank": i + 1, "pool_size": len(pool)}
    return {"rank": None, "pool_size": len(pool)}


def get_applicant(aid: int) -> dict | None:
    conn = connect()
    try:
        r = conn.execute("SELECT * FROM applicants WHERE id=?", (aid,)).fetchone()
        d = _row(r)
        if d:
            d["screening"] = _json_loads(d.get("screening") or "")
            d["notes"] = [_row(x) for x in conn.execute(
                "SELECT * FROM applicant_notes WHERE applicant_id=? ORDER BY created_at DESC",
                (aid,))]
            d["checks"] = [_row(x) for x in conn.execute(
                "SELECT * FROM applicant_checks WHERE applicant_id=? ORDER BY check_name",
                (aid,))]
            d.update(rank_info(aid))
        return d
    finally:
        conn.close()


def list_applicants(status: str | None = None, q: str | None = None) -> list[dict]:
    sql = "SELECT * FROM applicants"
    where, args = [], []
    if status:
        where.append("status=?")
        args.append(status)
    if q:
        where.append("(name LIKE ? OR phone LIKE ? OR email LIKE ? OR role_applied LIKE ?)")
        like = f"%{q}%"
        args += [like, like, like, like]
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY CASE status WHEN 'new' THEN 0 WHEN 'phone_screen' THEN 1 " \
           "WHEN 'interview' THEN 2 WHEN 'trial' THEN 3 WHEN 'pool' THEN 4 " \
           "WHEN 'hired' THEN 5 ELSE 6 END, created_at DESC"
    conn = connect()
    try:
        rows = [_row(r) for r in conn.execute(sql, args)]
        ranks = {d["id"]: i + 1 for i, d in enumerate(_ranked_pool())}
        pool_size = len(ranks)
        for d in rows:
            d["screening"] = _json_loads(d.get("screening") or "")
            d["rank"] = ranks.get(d["id"])
            d["pool_size"] = pool_size
        return rows
    finally:
        conn.close()


def update_applicant(aid: int, **fields) -> dict | None:
    allowed = {"name", "phone", "email", "source", "role_applied", "status",
               "verdict", "score", "notes"}
    if "status" in fields and fields["status"] not in APPLICANT_STATUSES:
        raise ValueError(f"status must be one of {APPLICANT_STATUSES}")
    sets, args = [], []
    for k, v in fields.items():
        if k in allowed and v is not None:
            sets.append(f"{k}=?")
            args.append(v)
    if not sets:
        return get_applicant(aid)
    if "status" in fields and fields["status"] == "hired":
        sets.append("hired_at=?")
        args.append(_now())
    sets.append("updated_at=?")
    args.append(_now())
    args.append(aid)
    conn = connect()
    try:
        conn.execute(f"UPDATE applicants SET {', '.join(sets)} WHERE id=?", args)
        conn.commit()
        return get_applicant(aid)
    finally:
        conn.close()


def set_screening(aid: int, report: dict) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE applicants SET screening=?, screening_ts=?, "
            "verdict=?, score=?, updated_at=? WHERE id=?",
            (json.dumps(report), _now(), report.get("verdict", ""),
             int(report.get("score", 0) or 0), _now(), aid))
        conn.commit()
    finally:
        conn.close()


def add_applicant_note(aid: int, author: str, note: str) -> dict:
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO applicant_notes (applicant_id, author, note, created_at) "
            "VALUES (?,?,?,?)", (aid, author, note, _now()))
        conn.commit()
        r = conn.execute("SELECT * FROM applicant_notes WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(r)
    finally:
        conn.close()


def get_applicant_checks(aid: int) -> list[dict]:
    conn = connect()
    try:
        return [_row(r) for r in conn.execute(
            "SELECT * FROM applicant_checks WHERE applicant_id=? ORDER BY check_name", (aid,))]
    finally:
        conn.close()


def ensure_checks(aid: int) -> list[dict]:
    """Create rows for all CHECK_NAMES if missing (idempotent), return them."""
    conn = connect()
    try:
        existing = {r["check_name"] for r in conn.execute(
            "SELECT check_name FROM applicant_checks WHERE applicant_id=?", (aid,))}
        for name in CHECK_NAMES:
            if name not in existing:
                conn.execute(
                    "INSERT INTO applicant_checks (applicant_id, check_name, done, updated_at) "
                    "VALUES (?,?,0,?)", (aid, name, _now()))
        conn.commit()
        return [_row(r) for r in conn.execute(
            "SELECT * FROM applicant_checks WHERE applicant_id=? ORDER BY check_name", (aid,))]
    finally:
        conn.close()


def set_check(aid: int, check_name: str, done: bool, note: str = "") -> dict | None:
    if check_name not in CHECK_NAMES:
        raise ValueError(f"check must be one of {CHECK_NAMES}")
    conn = connect()
    try:
        cur = conn.execute(
            "UPDATE applicant_checks SET done=?, note=?, updated_at=? "
            "WHERE applicant_id=? AND check_name=?",
            (1 if done else 0, note, _now(), aid, check_name))
        if cur.rowcount == 0:  # belt-and-braces: row missing -> insert
            conn.execute(
                "INSERT INTO applicant_checks (applicant_id, check_name, done, note, updated_at) "
                "VALUES (?,?,?,?,?)",
                (aid, check_name, 1 if done else 0, note, _now()))
        conn.commit()
        r = conn.execute(
            "SELECT * FROM applicant_checks WHERE applicant_id=? AND check_name=?",
            (aid, check_name)).fetchone()
        return _row(r)
    finally:
        conn.close()


def delete_applicant(aid: int) -> None:
    conn = connect()
    try:
        conn.execute("DELETE FROM applicants WHERE id=?", (aid,))
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Staff (retention)
# --------------------------------------------------------------------------
def create_staff(name: str, phone: str = "", email: str = "", role: str = "",
                 start_date: str = "", rate: str = "", supervisor: str = "",
                 site: str = "", skills: str = "", notes: str = "") -> int:
    now = _now()
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO staff (name, phone, email, role, start_date, rate, "
            "supervisor, site, skills, notes, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (name.strip(), phone.strip(), email.strip(), role.strip(),
             start_date.strip(), rate.strip(), supervisor.strip(), site.strip(),
             skills.strip(), notes.strip(), now, now))
        sid = cur.lastrowid
        # onboarding rows auto-created
        for m in STAFF_MILESTONES:
            conn.execute(
                "INSERT INTO staff_onboarding (staff_id, milestone, done, done_at) "
                "VALUES (?,?,0,'')", (sid, m))
        conn.commit()
        return sid
    finally:
        conn.close()


def _staff_detail(conn: sqlite3.Connection, sid: int) -> dict | None:
    r = conn.execute("SELECT * FROM staff WHERE id=?", (sid,)).fetchone()
    d = _row(r)
    if not d:
        return None
    d["onboarding"] = [_row(x) for x in conn.execute(
        "SELECT * FROM staff_onboarding WHERE staff_id=? ORDER BY milestone", (sid,))]
    d["stay_interviews"] = [_row(x) for x in conn.execute(
        "SELECT * FROM stay_interviews WHERE staff_id=? ORDER BY conducted_at DESC", (sid,))]
    d["flags"] = [_row(x) for x in conn.execute(
        "SELECT * FROM staff_flags WHERE staff_id=? ORDER BY created_at DESC", (sid,))]
    d["exits"] = [_row(x) for x in conn.execute(
        "SELECT * FROM exits WHERE staff_id=? ORDER BY created_at DESC", (sid,))]
    # risk: latest stay interview risk, else low
    d["risk"] = d["stay_interviews"][0]["risk"] if d["stay_interviews"] else "low"
    return d


def get_staff(sid: int) -> dict | None:
    conn = connect()
    try:
        return _staff_detail(conn, sid)
    finally:
        conn.close()


def list_staff(status: str | None = None, q: str | None = None) -> list[dict]:
    sql = "SELECT * FROM staff"
    where, args = [], []
    if status:
        where.append("status=?")
        args.append(status)
    if q:
        where.append("(name LIKE ? OR role LIKE ? OR site LIKE ?)")
        like = f"%{q}%"
        args += [like, like, like]
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY CASE WHEN status='left' THEN 1 ELSE 0 END, name"
    conn = connect()
    try:
        rows = []
        for r in conn.execute(sql, args):
            d = _staff_detail(conn, r["id"])
            if d:
                rows.append(d)
        return rows
    finally:
        conn.close()


def update_staff(sid: int, **fields) -> dict | None:
    allowed = {"name", "phone", "email", "role", "start_date", "rate",
               "supervisor", "site", "skills", "notes", "status"}
    sets, args = [], []
    for k, v in fields.items():
        if k in allowed and v is not None:
            sets.append(f"{k}=?")
            args.append(v)
    if sets:
        sets.append("updated_at=?")
        args.append(_now())
        args.append(sid)
        conn = connect()
        try:
            conn.execute(f"UPDATE staff SET {', '.join(sets)} WHERE id=?", args)
            conn.commit()
        finally:
            conn.close()
    return get_staff(sid)


def set_onboarding(sid: int, milestone: str, done: bool, note: str = "") -> None:
    if milestone not in STAFF_MILESTONES:
        raise ValueError(f"milestone must be one of {STAFF_MILESTONES}")
    conn = connect()
    try:
        conn.execute(
            "UPDATE staff_onboarding SET done=?, done_at=?, note=? "
            "WHERE staff_id=? AND milestone=?",
            (1 if done else 0, _now() if done else "", note, sid, milestone))
        conn.commit()
    finally:
        conn.close()


def add_stay_interview(sid: int, conducted_at: str, q_keep: str = "",
                       q_tempt: str = "", q_better: str = "",
                       risk: str = "low", note: str = "") -> dict:
    if risk not in RISK_LEVELS:
        raise ValueError(f"risk must be one of {RISK_LEVELS}")
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO stay_interviews (staff_id, conducted_at, q_keep, q_tempt, "
            "q_better, risk, note, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (sid, conducted_at, q_keep, q_tempt, q_better, risk, note, _now()))
        conn.commit()
        r = conn.execute("SELECT * FROM stay_interviews WHERE id=?",
                         (cur.lastrowid,)).fetchone()
        return dict(r)
    finally:
        conn.close()


def add_staff_flag(sid: int, kind: str, note: str) -> dict:
    if kind not in FLAG_KINDS:
        raise ValueError(f"kind must be one of {FLAG_KINDS}")
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO staff_flags (staff_id, kind, note, created_at) "
            "VALUES (?,?,?,?)", (sid, kind, note, _now()))
        conn.commit()
        r = conn.execute("SELECT * FROM staff_flags WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(r)
    finally:
        conn.close()


def resolve_flag(fid: int) -> None:
    conn = connect()
    try:
        conn.execute("UPDATE staff_flags SET resolved=1 WHERE id=?", (fid,))
        conn.commit()
    finally:
        conn.close()


def record_exit(sid: int, exit_date: str, reason: str = "other", note: str = "") -> dict:
    if reason not in EXIT_REASONS:
        raise ValueError(f"reason must be one of {EXIT_REASONS}")
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO exits (staff_id, exit_date, reason, note, created_at) "
            "VALUES (?,?,?,?,?)", (sid, exit_date, reason, note, _now()))
        conn.execute("UPDATE staff SET status='left', updated_at=? WHERE id=?",
                     (_now(), sid))
        conn.commit()
        r = conn.execute("SELECT * FROM exits WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(r)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
def dashboard() -> dict:
    conn = connect()
    try:
        active = conn.execute(
            "SELECT COUNT(*) c FROM staff WHERE status='active'").fetchone()["c"]
        left = conn.execute(
            "SELECT COUNT(*) c FROM staff WHERE status='left'").fetchone()["c"]
        by_status = {s: 0 for s in APPLICANT_STATUSES}
        for r in conn.execute("SELECT status, COUNT(*) c FROM applicants GROUP BY status"):
            by_status[r["status"]] = r["c"]
        at_risk = []
        for r in conn.execute("SELECT id, name, role FROM staff WHERE status='active'"):
            d = _staff_detail(conn, r["id"])
            if d and d["risk"] == "high":
                at_risk.append({"id": d["id"], "name": d["name"], "role": d["role"]})
        open_flags = conn.execute(
            "SELECT COUNT(*) c FROM staff_flags WHERE resolved=0").fetchone()["c"]
        # exit reasons aggregate (the 'three under same foreman' pattern view)
        exits_by_reason = {}
        for r in conn.execute(
                "SELECT e.reason, COUNT(*) c FROM exits e GROUP BY e.reason"):
            exits_by_reason[r["reason"]] = r["c"]
        return {
            "headcount": {"active": active, "left": left},
            "applicants_by_status": by_status,
            "at_risk": at_risk,
            "open_flags": open_flags,
            "exits_by_reason": exits_by_reason,
        }
    finally:
        conn.close()
