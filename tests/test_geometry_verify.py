"""Geometry verification pure-logic tests (no CadQuery required)."""
from __future__ import annotations

from pathlib import Path

from geometry_verify import VerificationReport, verify_geometry


def test_no_inputs_passes_with_warnings():
    report = verify_geometry(solid=None, stl_path=None)
    # No critical checks ran, no errors → passed
    assert report.passed is True
    assert any("No solid" in w for w in report.warnings)
    assert any("No STL" in w for w in report.warnings)


def test_missing_stl_fails():
    report = verify_geometry(solid=None, stl_path="/nonexistent/path.stl")
    assert report.passed is False
    assert report.checks.get("stl_present") is False
    assert any("not found" in e.lower() for e in report.errors)


def test_report_to_dict():
    r = VerificationReport(passed=True, checks={"a": True}, errors=[], warnings=["w"])
    d = r.to_dict()
    assert d["passed"] is True
    assert d["checks"]["a"] is True
    assert d["warnings"] == ["w"]
