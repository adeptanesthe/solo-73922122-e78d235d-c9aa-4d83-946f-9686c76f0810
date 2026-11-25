"""Pytest configuration: isolate SQLite into a temporary file BEFORE the
app package reads DB_PATH."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="exhibit-pytest-"))
os.environ["DB_PATH"] = str(_TMP / "test.db")
os.environ.pop("SEED_DEMO", None)

from fastapi.testclient import TestClient  # noqa: E402

from app.db import connect, init_db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _schema():
    init_db()
    yield


@pytest.fixture(autouse=True)
def clean_tables():
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM reading_chunks")
        conn.execute("DELETE FROM readings")
        conn.execute("DELETE FROM boxes")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('readings')")
        conn.execute("COMMIT")
    finally:
        conn.close()
    yield


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c
