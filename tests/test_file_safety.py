"""
Tests for file_safety.safe_output_path - the fix for the path-traversal
bug in the original /download/{step,stl}/{filename} routes.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from exceptions import NitocadError
from file_safety import safe_output_path


@pytest.fixture
def output_dir(tmp_path):
    d = tmp_path / "output"
    d.mkdir()
    (d / "shaft_abc123.step").write_text("fake step content")
    return d


def test_normal_filename_resolves_inside_output_dir(output_dir):
    result = safe_output_path(output_dir, "shaft_abc123.step")
    assert result == output_dir / "shaft_abc123.step"
    assert result.exists()


@pytest.mark.parametrize(
    "malicious",
    [
        "../../../etc/passwd",
        "../secrets.env",
        "..",
        ".",
        "",
        "sub/dir/file.step",
    ],
)
def test_path_traversal_attempts_are_rejected(output_dir, malicious):
    with pytest.raises(NitocadError):
        safe_output_path(output_dir, malicious)


def test_rejection_raises_400(output_dir):
    with pytest.raises(NitocadError) as exc_info:
        safe_output_path(output_dir, "../etc/passwd")
    assert exc_info.value.status_code == 400


def test_nonexistent_but_safe_filename_still_resolves(output_dir):
    # safe_output_path only validates the *path*, not whether the file
    # exists - existence is checked separately by the route handler so it
    # can return a clean 404 rather than conflating "unsafe" with "missing".
    result = safe_output_path(output_dir, "does_not_exist.step")
    assert result.parent == output_dir.resolve()
    assert not result.exists()
