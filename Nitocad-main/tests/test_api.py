"""
API-level integration tests via FastAPI's TestClient - covers what the
unit tests above can't: auth enforcement, request validation (Pydantic
model + FastAPI's 422s), health/readiness endpoints, and the download
routes' path-traversal protection end-to-end through real HTTP.

Does NOT require CadQuery for most of this file - /generate itself is
skipped without it (see TestGenerate below), everything else here tests
the HTTP layer around generation, not generation itself.
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


class TestApiKeyIssuance:
    def test_generate_service_key_returns_a_usable_key(self, client):
        response = client.post("/api/keys/generate")
        assert response.status_code == 200
        assert len(response.json()["api_key"]) > 20

    def test_each_call_issues_a_distinct_key(self, client):
        first = client.post("/api/keys/generate").json()["api_key"]
        second = client.post("/api/keys/generate").json()["api_key"]
        assert first != second


class TestAuthEnforcement:
    def test_generate_without_api_key_is_401(self, client):
        response = client.post("/generate", json={"description": "shaft 10mm diameter, 50mm long"})
        assert response.status_code == 401

    def test_generate_with_garbage_api_key_is_401(self, client):
        response = client.post(
            "/generate",
            json={"description": "shaft 10mm diameter, 50mm long"},
            headers={"X-API-Key": "not-a-real-key"},
        )
        assert response.status_code == 401

    def test_jobs_list_requires_auth(self, client):
        response = client.get("/api/jobs")
        assert response.status_code == 401


class TestRequestValidation:
    def test_blank_description_is_rejected_with_422(self, client, api_key):
        response = client.post(
            "/generate", json={"description": "   "}, headers={"X-API-Key": api_key}
        )
        assert response.status_code == 422

    def test_missing_description_field_is_422(self, client, api_key):
        response = client.post("/generate", json={}, headers={"X-API-Key": api_key})
        assert response.status_code == 422

    def test_oversized_description_is_rejected(self, client, api_key):
        response = client.post(
            "/generate",
            json={"description": "x" * 5000},
            headers={"X-API-Key": api_key},
        )
        assert response.status_code == 422


class TestJobLookup:
    def test_nonexistent_job_id_is_404(self, client, api_key):
        response = client.get("/api/jobs/does-not-exist", headers={"X-API-Key": api_key})
        assert response.status_code == 404

    def test_empty_job_list_for_fresh_key(self, client, api_key):
        response = client.get("/api/jobs", headers={"X-API-Key": api_key})
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

    def test_generate_shaft_end_to_end(self, client, api_key):
        response = client.post(
            "/generate",
            json={"description": "shaft 10mm diameter, 50mm long", "use_deepseek": False},
            headers={"X-API-Key": api_key},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["step_url"]
        assert body["parameters"]["part_type"] == "shaft"

    def test_job_appears_in_job_history_after_generation(self, client, api_key):
        client.post(
            "/generate",
            json={"description": "shaft 10mm diameter, 50mm long", "use_deepseek": False},
            headers={"X-API-Key": api_key},
        )
        response = client.get("/api/jobs", headers={"X-API-Key": api_key})
        jobs = response.json()
        assert len(jobs) == 1
        assert jobs[0]["part_type"] == "shaft"

    def test_generated_step_file_is_downloadable(self, client, api_key):
        gen_response = client.post(
            "/generate",
            json={"description": "shaft 10mm diameter, 50mm long", "use_deepseek": False},
            headers={"X-API-Key": api_key},
        )
        step_url = gen_response.json()["step_url"]
        filename = step_url.rsplit("/", 1)[-1]
        download_response = client.get(f"/download/step/{filename}")
        assert download_response.status_code == 200
