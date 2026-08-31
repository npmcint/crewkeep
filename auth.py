"""CrewKeep auth — scrypt password hashing + server-side sessions.

Trimmed from the xjobs (jobhunt) auth pattern: users(username, password_hash,
display_name, is_admin, status, created_at) + sessions(token_hash, username,
created_at, expires_at). First-user bootstrap: when the users table is empty,
POST /api/auth/register creates the FIRST admin; after that, admins add users
via the CLI (`crewkeep.py users add`) — no open self-registration for a
single-company app.

- scrypt (stdlib hashlib), PHC-ish stored format: scrypt$N$r$p$salt$hash
- Sessions: random 32-byte token stored SHA-256-hashed; cookie holds raw token
  (HttpOnly, SameSite=Lax).
- Fail-closed: no valid session => 401 everywhere.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("CREWKEEP_DATA", str(Path(__file__).parent / "data")))
DB_PATH = DATA_DIR / "users.db"

SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SESSION_TTL = timedelta(days=14)
COOKIE_NAME = "ck_session"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N,
                        r=SCRYPT_R, p=SCRYPT_P, dklen=32, maxmem=64 * 1024 * 1024)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        want = base64.b64decode(hash_b64)
        got = hashlib.scrypt(password.encode(), salt=salt,
                             n=int(n), r=int(r), p=int(p), dklen=len(want),
                             maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(got, want)
    except Exception:
        return False


def data_dir() -> Path:
    return Path(os.environ.get("CREWKEEP_DATA", str(Path(__file__).parent / "data")))


def connect() -> sqlite3.Connection:
    db_path = data_dir() / "users.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        username TEXT PRIMARY KEY,
        password_hash TEXT NOT NULL,
        display_name TEXT DEFAULT '',
        is_admin INTEGER DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS sessions (
        token_hash TEXT PRIMARY KEY,
        username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
        created_at TEXT,
        expires_at TEXT,
        user_agent TEXT DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(username);
    """)
    conn.commit()
    return conn


def user_count() -> int:
    conn = connect()
    try:
        return conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    finally:
        conn.close()


def _user_row(r: sqlite3.Row) -> dict:
    return {"username": r["username"], "display_name": r["display_name"],
            "is_admin": bool(r["is_admin"]), "status": r["status"],
            "created_at": r["created_at"]}


def create_user(username: str, password: str, display_name: str = "",
                is_admin: bool = False) -> dict:
    username = username.strip().lower()
    if not username or len(username) < 2:
        raise ValueError("username must be at least 2 characters")
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    conn = connect()
    try:
        if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            raise ValueError(f"user '{username}' already exists")
        conn.execute(
            "INSERT INTO users (username, password_hash, display_name, is_admin, status, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (username, _hash_password(password), display_name,
             1 if is_admin else 0, "active", _now().isoformat()))
        conn.commit()
        u = get_user(username)
        assert u is not None
        return u
    finally:
        conn.close()


def get_user(username: str) -> dict | None:
    conn = connect()
    try:
        r = conn.execute(
            "SELECT username, display_name, is_admin, status, created_at FROM users "
            "WHERE username=?", (username,)).fetchone()
        return _user_row(r) if r else None
    finally:
        conn.close()


def list_users() -> list[dict]:
    conn = connect()
    try:
        return [_user_row(r) for r in conn.execute(
            "SELECT username, display_name, is_admin, status, created_at FROM users ORDER BY username")]
    finally:
        conn.close()


def delete_user(username: str) -> None:
    conn = connect()
    try:
        conn.execute("DELETE FROM users WHERE username=?", (username,))
        conn.commit()
    finally:
        conn.close()


def set_password(username: str, new_password: str) -> None:
    if len(new_password) < 8:
        raise ValueError("password must be at least 8 characters")
    conn = connect()
    try:
        conn.execute("UPDATE users SET password_hash=? WHERE username=?",
                     (_hash_password(new_password), username))
        conn.commit()
    finally:
        conn.close()


def authenticate(username: str, password: str) -> dict | None:
    username = username.strip().lower()
    conn = connect()
    try:
        r = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not r or not _verify_password(password, r["password_hash"]):
            return None
        return _user_row(r)
    finally:
        conn.close()


def create_session(username: str, user_agent: str = "") -> str:
    raw = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    now = _now()
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO sessions (token_hash, username, created_at, expires_at, user_agent) "
            "VALUES (?,?,?,?,?)",
            (token_hash, username, now.isoformat(),
             (now + SESSION_TTL).isoformat(), user_agent[:200]))
        conn.commit()
    finally:
        conn.close()
    return raw


def user_for_token(raw_token: str) -> dict | None:
    if not raw_token:
        return None
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    conn = connect()
    try:
        r = conn.execute(
            "SELECT s.username, s.expires_at, u.display_name, u.is_admin "
            "FROM sessions s JOIN users u ON u.username = s.username "
            "WHERE s.token_hash=?", (token_hash,)).fetchone()
        if not r:
            return None
        if datetime.fromisoformat(r["expires_at"]) < _now():
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
            conn.commit()
            return None
        return {"username": r["username"], "display_name": r["display_name"],
                "is_admin": bool(r["is_admin"])}
    finally:
        conn.close()


def delete_session(raw_token: str) -> None:
    if not raw_token:
        return
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    conn = connect()
    try:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
        conn.commit()
    finally:
        conn.close()
