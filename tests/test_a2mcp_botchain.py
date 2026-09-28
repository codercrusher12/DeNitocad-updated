"""
Unit tests for a2mcp_botchain/server.py's MCP tools, post-migration to
wallet sessions + credits (see that module's own docstring for the
migration this covers). fastmcp's @mcp.tool decorator wraps these in a
Tool object but leaves the underlying function callable directly via
.fn - no MCP transport/client needed to test the actual logic, same
principle as test_api.py testing HTTP routes without a real browser.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


def _make_session(tmp_db, wallet_address="0xabc1230000000000000000000000000000000d"):
    session_id, _ = tmp_db.create_session(wallet_address, ttl_seconds=600)
    return wallet_address, session_id


class TestGenerateCadPartAuth:
    def test_invalid_session_id_is_a_clean_failure_not_an_exception(self, tmp_db):
        from a2mcp_botchain.server import generate_cad_part

        import asyncio
        result = asyncio.run(
            generate_cad_part.fn(description="a shaft", session_id="not-a-real-session")
        )
        assert result["success"] is False
        assert "session" in result["error"].lower()

    def test_zero_credits_is_a_clean_failure_not_an_exception(self, tmp_db):
        from a2mcp_botchain.server import generate_cad_part
        import asyncio

        _, session_id = _make_session(tmp_db)
        result = asyncio.run(
            generate_cad_part.fn(description="a shaft", session_id=session_id)
        )
        assert result["success"] is False
        assert "credit" in result["error"].lower()

    def test_zero_credits_does_not_touch_the_balance(self, tmp_db):
        """A failed eligibility check must not itself consume anything -
        this pins that down the same way test_api.py's
        test_rejected_requests_do_not_spend_a_credit does for the HTTP
        path."""
        from a2mcp_botchain.server import generate_cad_part
        import asyncio

        wallet_address, session_id = _make_session(tmp_db)
        asyncio.run(generate_cad_part.fn(description="a shaft", session_id=session_id))
        assert tmp_db.get_credit_balance(wallet_address) == 0


class TestExportFormatAuth:
    def test_invalid_session_id_is_a_clean_failure(self, tmp_db):
        from a2mcp_botchain.server import export_format
        import asyncio

        result = asyncio.run(
            export_format.fn(job_id="whatever", fmt="step", session_id="not-a-real-session")
        )
        assert result["success"] is False

    def test_nonexistent_job_is_a_clean_failure(self, tmp_db):
        from a2mcp_botchain.server import export_format
        import asyncio

        _, session_id = _make_session(tmp_db)
        result = asyncio.run(
            export_format.fn(job_id="does-not-exist", fmt="step", session_id=session_id)
        )
        assert result["success"] is False
        assert result["error"] == "Job not found."

    def test_someone_elses_job_is_reported_as_not_found(self, tmp_db):
        """Same ownership-privacy property as
        test_api.py's test_export_of_someone_elses_job_is_404: a wallet
        that doesn't own a job gets the same "not found" a nonexistent
        job would get, not a distinguishable "forbidden" - so this
        can't be used to probe which job IDs exist for other wallets."""
        from a2mcp_botchain.server import export_format
        import asyncio

        owner_wallet = "0x0000000000000000000000000000000000aaaa"
        job_id = tmp_db.record_job(
            user_id=owner_wallet,
            description="a shaft",
            part_type="shaft",
            parameters={"diameter_mm": 10, "length_mm": 50},
            material=None,
            used_deepseek=False,
            success=True,
            error=None,
            step_url=None,
            stl_url="/download/stl/shaft_abc.stl",
            warnings=[],
            corrections={},
        )
        _, other_session_id = _make_session(tmp_db, wallet_address="0x0000000000000000000000000000000000bbbb")
        result = asyncio.run(
            export_format.fn(job_id=job_id, fmt="step", session_id=other_session_id)
        )
        assert result["success"] is False
        assert result["error"] == "Job not found."


class TestGenerateCadPartHappyPath:
    """Requires CadQuery - skips cleanly otherwise, matching
    test_api.py's TestGenerateHappyPath."""

    @pytest.fixture(autouse=True)
    def _require_cadquery(self):
        pytest.importorskip("cadquery", reason="requires a real CadQuery/OCCT install")

    def test_generate_spends_exactly_one_credit(self, tmp_db):
        from a2mcp_botchain.server import generate_cad_part
        import asyncio

        wallet_address, session_id = _make_session(tmp_db)
        tmp_db.add_credits(wallet_address, 5)

        result = asyncio.run(
            generate_cad_part.fn(
                description="shaft 10mm diameter, 50mm long", session_id=session_id
            )
        )
        assert result["success"] is True
        assert tmp_db.get_credit_balance(wallet_address) == 4

    def test_export_of_own_job_does_not_spend_a_credit(self, tmp_db):
        from a2mcp_botchain.server import generate_cad_part, export_format
        import asyncio

        wallet_address, session_id = _make_session(tmp_db)
        tmp_db.add_credits(wallet_address, 5)

        gen_result = asyncio.run(
            generate_cad_part.fn(
                description="shaft 10mm diameter, 50mm long", session_id=session_id
            )
        )
        balance_before_export = tmp_db.get_credit_balance(wallet_address)

        export_result = asyncio.run(
            export_format.fn(job_id=gen_result["job_id"], fmt="step", session_id=session_id)
        )
        assert export_result["success"] is True
        assert tmp_db.get_credit_balance(wallet_address) == balance_before_export
