"""
API-level integration tests via FastAPI's TestClient - covers what the
unit tests above can't: auth enforcement, request validation (Pydantic
model + FastAPI's 422s), health/readiness endpoints, and the download
routes' path-traversal protection end-to-end through real HTTP.

Does NOT require CadQuery for most of this file - /generate itself is
skipped without it (see TestGenerate below), everything else here tests
the HTTP layer around generation, not generation itself.

Auth model: wallet sign-in (POST /auth/nonce -> sign -> POST /auth/verify)
replaced API keys entirely - see wallet_auth.py and db.py's module
docstrings. The wallet_session/wallet_with_credits fixtures in
conftest.py do the real sign-in flow through the actual HTTP endpoints,
same principle as the old api_key fixture they replaced.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


class TestHealthEndpoints:
    def test_healthz_is_public_and_returns_ok(self, client):
        response = client.get("/healthz")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"

    def test_readyz_reports_database_check(self, client):
        response = client.get("/readyz")
        assert response.status_code == 200
        assert response.json()["checks"]["database"] is True


class TestWalletAuth:
    def test_nonce_returns_a_message_containing_the_nonce(self, client):
        response = client.post("/auth/nonce", json={"wallet_address": "0x" + "1" * 40})
        assert response.status_code == 200
        body = response.json()
        assert body["nonce"] in body["message"]

    def test_nonce_rejects_an_invalid_address(self, client):
        response = client.post("/auth/nonce", json={"wallet_address": "not-an-address"})
        assert response.status_code == 422

    def test_sign_in_flow_issues_a_working_session(self, client, wallet_session):
        # wallet_session fixture already ran the full flow and asserted
        # 200s along the way - this test is really about what comes
        # after: the session it returns should actually authenticate.
        response = client.get("/auth/me", headers=wallet_session["auth_headers"])
        assert response.status_code == 200
        assert response.json()["wallet_address"] == wallet_session["wallet_address"]

    def test_verify_rejects_a_reused_nonce(self, client):
        from eth_account import Account
        from eth_account.messages import encode_defunct

        account = Account.create()
        nonce_data = client.post("/auth/nonce", json={"wallet_address": account.address}).json()
        signed = Account.sign_message(encode_defunct(text=nonce_data["message"]), private_key=account.key)
        body = {
            "wallet_address": account.address,
            "nonce": nonce_data["nonce"],
            "signature": signed.signature.hex(),
        }
        first = client.post("/auth/verify", json=body)
        assert first.status_code == 200
        replay = client.post("/auth/verify", json=body)
        assert replay.status_code == 401

    def test_verify_rejects_a_signature_from_a_different_wallet(self, client):
        from eth_account import Account
        from eth_account.messages import encode_defunct

        claimed = Account.create()
        actual_signer = Account.create()
        nonce_data = client.post("/auth/nonce", json={"wallet_address": claimed.address}).json()
        signed = Account.sign_message(
            encode_defunct(text=nonce_data["message"]), private_key=actual_signer.key
        )
        response = client.post(
            "/auth/verify",
            json={
                "wallet_address": claimed.address,
                "nonce": nonce_data["nonce"],
                "signature": signed.signature.hex(),
            },
        )
        assert response.status_code == 401

    def test_logout_invalidates_the_session(self, client, wallet_session):
        logout = client.post("/auth/logout", headers=wallet_session["auth_headers"])
        assert logout.status_code == 200
        after = client.get("/auth/me", headers=wallet_session["auth_headers"])
        assert after.status_code == 401


class TestAuthEnforcement:
    def test_generate_without_session_is_401(self, client):
        response = client.post("/generate", json={"description": "shaft 10mm diameter, 50mm long"})
        assert response.status_code == 401

    def test_generate_with_garbage_bearer_token_is_401(self, client):
        response = client.post(
            "/generate",
            json={"description": "shaft 10mm diameter, 50mm long"},
            headers={"Authorization": "Bearer not-a-real-session"},
        )
        assert response.status_code == 401

    def test_jobs_list_requires_auth(self, client):
        response = client.get("/api/jobs")
        assert response.status_code == 401


class TestCredits:
    def test_fresh_wallet_has_zero_balance(self, client, wallet_session):
        response = client.get("/credits/balance", headers=wallet_session["auth_headers"])
        assert response.status_code == 200
        assert response.json()["balance"] == 0

    def test_generate_with_zero_credits_is_402(self, client, wallet_session):
        response = client.post(
            "/generate",
            json={"description": "shaft 10mm diameter, 50mm long"},
            headers=wallet_session["auth_headers"],
        )
        assert response.status_code == 402

    def test_purchase_rejects_an_unknown_tier(self, client, wallet_session):
        response = client.post(
            "/credits/purchase",
            json={"tx_hash": "0x" + "0" * 64, "tier": "not_a_real_tier"},
            headers=wallet_session["auth_headers"],
        )
        assert response.status_code == 422

    def test_purchase_requires_auth(self, client):
        response = client.post(
            "/credits/purchase", json={"tx_hash": "0x" + "0" * 64, "tier": "single"}
        )
        assert response.status_code == 401


class TestRequestValidation:
    def test_blank_description_is_rejected_with_422(self, client, wallet_with_credits):
        response = client.post(
            "/generate", json={"description": "   "}, headers=wallet_with_credits["auth_headers"]
        )
        assert response.status_code == 422

    def test_missing_description_field_is_422(self, client, wallet_with_credits):
        response = client.post("/generate", json={}, headers=wallet_with_credits["auth_headers"])
        assert response.status_code == 422

    def test_oversized_description_is_rejected(self, client, wallet_with_credits):
        response = client.post(
            "/generate",
            json={"description": "x" * 5000},
            headers=wallet_with_credits["auth_headers"],
        )
        assert response.status_code == 422

    def test_rejected_requests_do_not_spend_a_credit(self, client, wallet_with_credits):
        """A 422 happens before db.consume_credit is ever called - this
        pins that down so a future refactor can't accidentally move the
        credit spend earlier and start charging for invalid requests."""
        before = client.get("/credits/balance", headers=wallet_with_credits["auth_headers"]).json()
        client.post("/generate", json={}, headers=wallet_with_credits["auth_headers"])
        after = client.get("/credits/balance", headers=wallet_with_credits["auth_headers"]).json()
        assert after["balance"] == before["balance"]


class TestJobLookup:
    def test_nonexistent_job_id_is_404(self, client, wallet_with_credits):
        response = client.get("/api/jobs/does-not-exist", headers=wallet_with_credits["auth_headers"])
        assert response.status_code == 404

    def test_empty_job_list_for_fresh_wallet(self, client, wallet_with_credits):
        response = client.get("/api/jobs", headers=wallet_with_credits["auth_headers"])
        assert response.status_code == 200
        assert response.json() == []


class TestDownloadPathSafety:
    def test_traversal_attempt_on_step_download_is_400(self, client):
        response = client.get("/download/step/..%2F..%2F..%2Fetc%2Fpasswd")
        # Starlette/FastAPI normalize the path before routing reaches our
        # handler in some configurations; either a 400 (our own check
        # fired) or a 404 (nothing matched the route / file not found) is
        # an acceptable outcome - a 200 leaking file content is not.
        assert response.status_code in (400, 404)
        assert "root:" not in response.text

    def test_nonexistent_but_safe_filename_is_404(self, client):
        response = client.get("/download/step/does_not_exist.step")
        assert response.status_code == 404

    def test_stl_download_same_protection(self, client):
        response = client.get("/download/stl/does_not_exist.stl")
        assert response.status_code == 404


class TestGenerateHappyPath:
    """Requires CadQuery (the /generate route calls straight through to
    CADGenerator). Skips cleanly if it isn't installed, matching
    test_cad_templates.py's approach."""

    @pytest.fixture(autouse=True)
    def _require_cadquery(self):
        pytest.importorskip("cadquery", reason="requires a real CadQuery/OCCT install")

    def test_generate_shaft_end_to_end(self, client, wallet_with_credits):
        response = client.post(
            "/generate",
            json={"description": "shaft 10mm diameter, 50mm long", "use_deepseek": False},
            headers=wallet_with_credits["auth_headers"],
        )
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["step_url"]
        assert body["parameters"]["part_type"] == "shaft"

    def test_generate_spends_exactly_one_credit(self, client, wallet_with_credits):
        before = client.get("/credits/balance", headers=wallet_with_credits["auth_headers"]).json()
        client.post(
            "/generate",
            json={"description": "shaft 10mm diameter, 50mm long", "use_deepseek": False},
            headers=wallet_with_credits["auth_headers"],
        )
        after = client.get("/credits/balance", headers=wallet_with_credits["auth_headers"]).json()
        assert after["balance"] == before["balance"] - 1

    def test_job_appears_in_job_history_after_generation(self, client, wallet_with_credits):
        client.post(
            "/generate",
            json={"description": "shaft 10mm diameter, 50mm long", "use_deepseek": False},
            headers=wallet_with_credits["auth_headers"],
        )
        response = client.get("/api/jobs", headers=wallet_with_credits["auth_headers"])
        jobs = response.json()
        assert len(jobs) == 1
        assert jobs[0]["part_type"] == "shaft"

    def test_generated_step_file_is_downloadable(self, client, wallet_with_credits):
        gen_response = client.post(
            "/generate",
            json={"description": "shaft 10mm diameter, 50mm long", "use_deepseek": False},
            headers=wallet_with_credits["auth_headers"],
        )
        step_url = gen_response.json()["step_url"]
        filename = step_url.rsplit("/", 1)[-1]
        download_response = client.get(f"/download/step/{filename}")
        assert download_response.status_code == 200

    def test_export_does_not_consume_an_additional_credit(self, client, wallet_with_credits):
        """Regression test for the exact bug reported earlier: "charged
        0.2 BOT per format on top of the 0.2 BOT generation charge, for
        one job." Exports of an already-generated job must be free -
        see GET /export/{fmt}/{job_id}'s docstring in web_app.py."""
        gen_response = client.post(
            "/generate",
            json={"description": "shaft 10mm diameter, 50mm long", "use_deepseek": False},
            headers=wallet_with_credits["auth_headers"],
        )
        job_id = gen_response.json()["job_id"]
        balance_before_export = client.get(
            "/credits/balance", headers=wallet_with_credits["auth_headers"]
        ).json()["balance"]

        export_response = client.get(
            f"/export/step/{job_id}", headers=wallet_with_credits["auth_headers"]
        )
        assert export_response.status_code == 200

        balance_after_export = client.get(
            "/credits/balance", headers=wallet_with_credits["auth_headers"]
        ).json()["balance"]
        assert balance_after_export == balance_before_export

    def test_export_of_someone_elses_job_is_404(self, client, wallet_with_credits, tmp_db):
        """Ownership check: a different wallet's session must not be
        able to pull another wallet's job, even with valid credits of
        its own."""
        from eth_account import Account
        from eth_account.messages import encode_defunct

        gen_response = client.post(
            "/generate",
            json={"description": "shaft 10mm diameter, 50mm long", "use_deepseek": False},
            headers=wallet_with_credits["auth_headers"],
        )
        job_id = gen_response.json()["job_id"]

        other_account = Account.create()
        nonce_data = client.post("/auth/nonce", json={"wallet_address": other_account.address}).json()
        signed = Account.sign_message(
            encode_defunct(text=nonce_data["message"]), private_key=other_account.key
        )
        other_session = client.post(
            "/auth/verify",
            json={
                "wallet_address": other_account.address,
                "nonce": nonce_data["nonce"],
                "signature": signed.signature.hex(),
            },
        ).json()

        response = client.get(
            f"/export/step/{job_id}",
            headers={"Authorization": "Bearer " + other_session["session_id"]},
        )
        assert response.status_code == 404
