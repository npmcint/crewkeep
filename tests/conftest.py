"""Test isolation: every test gets a FRESH data dir (env var is read at
call-time by auth/db/app, so swapping it per-test works without reloads)."""
import os
import tempfile

import pytest

# import-time fallback so collection never explodes
os.environ.setdefault("CREWKEEP_DATA", tempfile.mkdtemp(prefix="crewkeep_test_"))
os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    os.environ["CREWKEEP_DATA"] = str(tmp_path)
    yield
