"""Job state machine transitions and refund path."""
from __future__ import annotations

import pytest

import db


def test_happy_path_transitions(tmp_db):
    job_id = db.create_job_reserved(description="cube 10mm", user_id="0xabc")
    job = db.get_job(job_id)
    assert job["state"] == "reserved"

    for state in ("parsed", "spec_valid", "built", "verified", "exported", "settled"):
        db.transition_job_state(job_id, state)
        assert db.get_job(job_id)["state"] == state

    final = db.get_job(job_id)
    assert final["success"] == 1


def test_illegal_transition_raises(tmp_db):
    job_id = db.create_job_reserved(description="x", user_id="u")
    with pytest.raises(ValueError, match="Illegal"):
        db.transition_job_state(job_id, "settled")


def test_failure_then_refund(tmp_db):
    job_id = db.create_job_reserved(description="x", user_id="u")
    db.transition_job_state(job_id, "failed", error="boom")
    assert db.get_job(job_id)["state"] == "failed"
    assert db.get_job(job_id)["success"] == 0

    db.transition_job_state(job_id, "refunded")
    assert db.get_job(job_id)["state"] == "refunded"


def test_record_job_defaults_to_settled_or_failed(tmp_db):
    ok = db.record_job(description="ok", success=True, user_id="u", part_type="plate")
    assert db.get_job(ok)["state"] == "settled"
    bad = db.record_job(description="bad", success=False, user_id="u", error="e")
    assert db.get_job(bad)["state"] == "failed"


def test_verification_report_persisted(tmp_db):
    job_id = db.create_job_reserved(description="x", user_id="u")
    report = {"passed": True, "checks": {"volume_positive": True}, "errors": []}
    for s in ("parsed", "spec_valid", "built"):
        db.transition_job_state(job_id, s)
    db.transition_job_state(job_id, "verified", verification_report=report)
    row = db.get_job(job_id)
    assert row["state"] == "verified"
    assert "volume_positive" in (row.get("verification_report") or "")
