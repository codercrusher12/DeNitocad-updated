"""
Shared pytest fixtures.

The original codebase had no test suite at all beyond `smoke_test.py` (a
standalone script meant to be run manually against a real deploy, not
picked up by pytest). Every fixture here exists to make the rest of the
suite runnable repeatedly and in parallel without side effects:

- `tmp_db`: points db.DB_PATH at a fresh sqlite file per test instead of
  the real `nl_to_cad.db` in the repo root - without this, running tests
  would create/pollute a real database file on disk and tests would leak
  state into each other via shared rows.
- `tmp_output_dir`: same idea for generated STEP/STL files.
- `client`: a FastAPI TestClient wired to the same temp DB/output dir,
  for exercising the actual HTTP routes (auth, rate limiting, validation
  errors) rather than only the underlying Python functions.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Fresh sqlite file per test, via config.settings rather than
    reaching into db.DB_PATH directly - keeps the fixture correct even
    if db.py's own DB_PATH assignment changes shape later."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_path))

    import config

    config.get_settings.cache_clear()

    import db

    importlib.reload(db)
    db.init_db()
    yield db
    config.get_settings.cache_clear()


@pytest.fixture
def tmp_output_dir(tmp_path):
    out = tmp_path / "output"
    out.mkdir()
    return out


@pytest.fixture
def client(tmp_db, tmp_output_dir, monkeypatch):
    """TestClient wired to isolated DB/output dirs. Imports web_app lazily
    (after env vars are patched) since web_app.py builds module-level
    objects (the CADGenerator, the Limiter) at import time."""
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_output_dir))
    monkeypatch.setenv("ENVIRONMENT", "development")

    import config

    config.get_settings.cache_clear()

    import web_app

    importlib.reload(web_app)

    from fastapi.testclient import TestClient

    with TestClient(web_app.app) as test_client:
        yield test_client


@pytest.fixture
def api_key(client):
    """Issues a real service API key through the actual endpoint, so
    tests exercise the same code path a real caller would instead of
    reaching into db.create_api_key() directly."""
    response = client.post("/api/keys/generate")
    assert response.status_code == 200
    return response.json()["api_key"]
