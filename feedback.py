"""In-app feedback & feature requests (mirrors the jobhunt pattern).

Any logged-in user can submit a suggestion (feature request / bug / other);
admins review it in the UI (approve / reject / mark done, with a note).
Items are never edited after submission.

Stored in <CREWKEEP_DATA>/feedback.db (SQLite, WAL). The data dir is read at
CALL time (never at import) so tests and prod both work with the same module —
same convention as auth.py / db.py.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

CATEGORIES = ("feature", "bug", "other")
# lifecycle: new -> approved -> done | rejected
STATUSES = ("new", "approved", "rejected", "done")
MAX_MESSAGE = 2000


def data_dir() -> Path:
    return Path(os.environ.get("CREWKEEP_DATA", str(Path(__file__).parent / "data")))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    p = Path(db_path) if db_path is not None else data_dir() / "feedback.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        user_id TEXT NOT NULL,
        category TEXT NOT NULL,
        message TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'new',
        admin_note TEXT DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_feedback_status ON feedback(status);
    CREATE INDEX IF NOT EXISTS idx_feedback_user ON feedback(user_id);
    """)
    return conn


def submit(user_id: str, category: str, message: str,
           db_path: str | Path | None = None) -> int:
    """Create a feedback item. Returns its id. Raises ValueError on bad input."""
    user_id = (user_id or "").strip().lower()
    category = (category or "").strip().lower()
    message = (message or "").strip()
    if not user_id:
        raise ValueError("unknown user")
    if category not in CATEGORIES:
        raise ValueError(f"category must be one of {', '.join(CATEGORIES)}")
    if not message:
        raise ValueError("message cannot be empty")
    if len(message) > MAX_MESSAGE:
        raise ValueError(f"message too long (max {MAX_MESSAGE} chars)")
    conn = connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO feedback (ts, user_id, category, message, status) "
            "VALUES (?,?,?,?, 'new')",
            (_now(), user_id, category, message))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _row(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "ts": r["ts"], "user_id": r["user_id"],
            "category": r["category"], "message": r["message"],
            "status": r["status"], "admin_note": r["admin_note"] or ""}


def get_item(item_id: int, db_path: str | Path | None = None) -> dict | None:
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT * FROM feedback WHERE id=?", (item_id,)).fetchone()
        return _row(r) if r else None
    finally:
        conn.close()


def list_items(status: str | None = None, limit: int = 100,
               db_path: str | Path | None = None) -> list[dict]:
    """All items (admin view): newest first; optional status filter."""
    conn = connect(db_path)
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM feedback WHERE status=? ORDER BY ts DESC, id DESC LIMIT ?",
                (status, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM feedback ORDER BY ts DESC, id DESC LIMIT ?",
                (limit,)).fetchall()
        return [_row(r) for r in rows]
    finally:
        conn.close()


def mine(user_id: str, limit: int = 20,
         db_path: str | Path | None = None) -> list[dict]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM feedback WHERE user_id=? ORDER BY ts DESC, id DESC LIMIT ?",
            (user_id.strip().lower(), limit)).fetchall()
        return [_row(r) for r in rows]
    finally:
        conn.close()


def set_status(item_id: int, status: str, admin_note: str = "",
               db_path: str | Path | None = None) -> dict:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    conn = connect(db_path)
    try:
        cur = conn.execute(
            "UPDATE feedback SET status=?, admin_note=? WHERE id=?",
            (status, (admin_note or "").strip(), item_id))
        if cur.rowcount == 0:
            raise ValueError(f"feedback item {item_id} not found")
        conn.commit()
        return _row(conn.execute("SELECT * FROM feedback WHERE id=?",
                                 (item_id,)).fetchone())
    finally:
        conn.close()


def counts(db_path: str | Path | None = None) -> dict:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) c FROM feedback GROUP BY status").fetchall()
        return {r["status"]: r["c"] for r in rows}
    finally:
        conn.close()
