import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import auth


def test_hash_verify_roundtrip():
    h = auth._hash_password("hunter2secure")
    assert h.startswith("scrypt$")
    assert auth._verify_password("hunter2secure", h)
    assert not auth._verify_password("wrong", h)
    assert not auth._verify_password("hunter2secure", "garbage")


def test_create_and_authenticate():
    u = auth.create_user("boss", "password1", "The Boss", is_admin=True)
    assert u["username"] == "boss"
    assert u["is_admin"] is True
    got = auth.authenticate("BOSS", "password1")
    assert got and got["username"] == "boss"
    assert auth.authenticate("boss", "nope") is None


def test_user_validation():
    with pytest.raises(ValueError):
        auth.create_user("x", "password1")
    with pytest.raises(ValueError):
        auth.create_user("boss2", "short")
    auth.create_user("dup", "password1")
    with pytest.raises(ValueError):
        auth.create_user("dup", "password1")  # duplicate


def test_session_lifecycle():
    auth.create_user("u1", "password1")
    raw = auth.create_session("u1")
    u = auth.user_for_token(raw)
    assert u["username"] == "u1"
    auth.delete_session(raw)
    assert auth.user_for_token(raw) is None


def test_session_expiry():
    auth.create_user("u2", "password1")
    raw = auth.create_session("u2")
    conn = auth.connect()
    conn.execute(
        "UPDATE sessions SET expires_at=?",
        ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),))
    conn.commit()
    conn.close()
    assert auth.user_for_token(raw) is None  # expired -> purged


def test_password_change():
    auth.create_user("u3", "password1")
    auth.set_password("u3", "newpass99")
    assert auth.authenticate("u3", "newpass99") is not None
    assert auth.authenticate("u3", "password1") is None


def test_delete_user_cascades_sessions():
    auth.create_user("u4", "password1")
    raw = auth.create_session("u4")
    auth.delete_user("u4")
    assert auth.user_for_token(raw) is None
