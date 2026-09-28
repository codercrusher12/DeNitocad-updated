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
- `wallet_session`: signs a fresh throwaway wallet in through the real
  POST /auth/nonce -> POST /auth/verify flow - the wallet-session
  equivalent of the old api_key fixture (retired along with the whole
  API-key system; see db.py and web_app.py's own notes on that).
- `wallet_with_credits`: wallet_session plus 1000 pre-loaded credits,
  for tests that need to actually call /generate without also
  exercising real on-chain payment verification.
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
def wallet_session(client):
    """Signs in a fresh test wallet through the actual HTTP endpoints
    (POST /auth/nonce -> sign -> POST /auth/verify), so tests exercise
    the same code path a real caller would - same spirit as the old
    api_key fixture it replaces, which issued a real key through
    POST /api/keys/generate rather than reaching into db.py directly.

    Uses eth_account to generate a real throwaway keypair and sign the
    nonce message for real - this is free (no gas, no chain
    interaction), unlike the old fixture which needed a fake on-chain
    payment.

    Returns a dict with wallet_address, session_id, and a ready-to-use
    auth_headers dict, since nearly every caller needs the last one.
    """
    from eth_account import Account
    from eth_account.messages import encode_defunct

    account = Account.create()

    nonce_resp = client.post("/auth/nonce", json={"wallet_address": account.address})
    assert nonce_resp.status_code == 200, nonce_resp.text
    nonce_data = nonce_resp.json()

    signed = Account.sign_message(encode_defunct(text=nonce_data["message"]), private_key=account.key)

    verify_resp = client.post(
        "/auth/verify",
        json={
            "wallet_address": account.address,
            "nonce": nonce_data["nonce"],
            "signature": signed.signature.hex(),
        },
    )
    assert verify_resp.status_code == 200, verify_resp.text
    session = verify_resp.json()

    return {
        "wallet_address": session["wallet_address"],
        "session_id": session["session_id"],
        "auth_headers": {"Authorization": "Bearer " + session["session_id"]},
    }


@pytest.fixture
def wallet_with_credits(wallet_session, tmp_db):
    """wallet_session, but pre-loaded with credits by writing directly
    to wallet_credits via db.add_credits - the same pragmatic shortcut
    tmp_db itself takes for setup (a real POST /credits/purchase needs
    a real on-chain payment, which is out of scope for these tests;
    that endpoint's own logic is exercised separately, see
    TestCredits). 1000 credits is comfortably more than any single
    test spends."""
    tmp_db.add_credits(wallet_session["wallet_address"], 1000)
    return wallet_session
