"""Freeform fidelity gate - pure logic, no third-party dependencies."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from freeform_validation import (  # noqa: E402
    needs_ai_parser, normalize_freeform, validate_freeform, validate_llm_response,
)

SHARK = ("A freeform shark-fin-shaped flat plate, 50mm tall and 30mm long at the base. "
         "The leading edge (front) is a curved sweep, and the trailing edge (back) curves "
         "inward (concave). Constant thickness of 5mm, sharp corners.")
PATH5 = [[0, 0, 0], [0, 0, 5]]
CONVEX_8 = [[0, 0], [30, 0], [28, 10], [24, 20], [18, 30], [12, 40], [6, 47], [0, 50]]
CONCAVE_9 = [[0, 0], [30, 0], [27, 8], [22, 14], [19, 22], [20, 32], [18, 42], [12, 47], [0, 50]]
SQUARE = [[-5, -5], [5, -5], [5, 5], [-5, 5]]


def sweep(profile, path=PATH5):
    return {"operation": "sweep", "profile_points": profile, "path_points": path}


# ---- the shark-fin regression (both real outputs from the bug report) ------

def test_triangle_is_rejected_for_curved_description():
    errs = validate_freeform(sweep([[0, 0], [30, 0], [30, 50], [0, 0]]), SHARK)
    assert any("at least 8" in e for e in errs)

def test_entirely_convex_curve_is_rejected_when_concave_requested():
    errs = validate_freeform(sweep(CONVEX_8), SHARK)
    assert any("entirely convex" in e for e in errs)

def test_genuinely_concave_fin_is_accepted():
    assert validate_freeform(sweep(CONCAVE_9), SHARK) == []


# ---- things the previous gate wrongly rejected ------------------------------

def test_loft_with_profiles_only_is_accepted():
    loft = {"operation": "loft", "profiles": [
        {"points": [[-10, -10], [10, -10], [10, 10], [-10, 10]], "z": 0},
        {"points": [[-5, -5], [5, -5], [5, 5], [-5, 5]], "z": 20}]}
    assert validate_freeform(loft, "A tapered loft from a 20mm square to a 10mm square, 20mm tall") == []

def test_revolve_wide_means_diameter():
    post = {"operation": "revolve", "revolve_axis": "Y",
            "profile_points": [[0, 0], [15, 0], [15, 50], [0, 50]]}
    assert validate_freeform(post, "A cylindrical post 50mm tall and 30mm wide") == []
    wrong = dict(post, profile_points=[[0, 0], [30, 0], [30, 50], [0, 50]])
    assert validate_freeform(wrong, "A cylindrical post 50mm tall and 30mm wide")

def test_long_sweep_of_small_section_is_accepted():
    bar = sweep(SQUARE, [[0, 0, 0], [0, 0, 200]])
    assert validate_freeform(bar, "A square bar 10mm wide, 200mm long, made by a sweep") == []

def test_plain_rectangle_not_forced_to_eight_points_by_negated_or_incidental_words():
    rect = sweep([[0, 0], [40, 0], [40, 20], [0, 20]])
    assert validate_freeform(rect, "A flat plate 40mm long, 20mm tall, no convex features") == []
    assert validate_freeform(rect, "A swept flat plate 40mm long and 20mm tall") == []


# ---- new structural checks ---------------------------------------------------

def test_self_intersecting_profile_rejected():
    assert validate_freeform(sweep([[0, 0], [40, 20], [40, 0], [0, 20]]), "plate 40mm long, 20mm tall")
    star = [[0, 0], [10, 0], [10, 10], [5, -5], [0, 10]]
    assert any("self-intersecting" in e for e in validate_freeform(sweep(star), "plate"))

def test_revolve_profile_crossing_axis_rejected():
    bad = {"operation": "revolve", "revolve_axis": "Y",
           "profile_points": [[-5, 0], [15, 0], [15, 50], [-5, 50]]}
    assert any("crosses" in e for e in validate_freeform(bad, "post"))

def test_sweep_path_with_y_rejected_not_silently_flattened():
    errs = validate_freeform(sweep(SQUARE, [[0, 0, 0], [0, 10, 5]]), "bar")
    assert any("y=0" in e for e in errs)

def test_loft_needs_distinct_z():
    p = {"points": SQUARE, "z": 0}
    assert validate_freeform({"operation": "loft", "profiles": [p, dict(p)]}, "loft")

def test_stated_dimension_mismatch_rejected():
    assert validate_freeform(sweep([[0, 0], [40, 0], [40, 20], [0, 20]]), "A plate 40mm long and 90mm tall")

def test_non_mm_units_are_not_checked_against_mm_regexes():
    rect = sweep([[0, 0], [50.8, 0], [50.8, 25.4], [0, 25.4]])
    assert validate_freeform(rect, "A plate 2 inches long and 1 inch tall") == []


# ---- normalisation and top-level shape --------------------------------------

def test_repeated_closing_point_is_normalised_not_rejected():
    out, errs = validate_llm_response(
        {"part_type": "freeform",
         "parameters": sweep([[0, 0], [40, 0], [40, 20], [0, 20], [0, 0]]), "confidence": "0.9"},
        "A plate 40mm long and 20mm tall")
    assert errs == []
    assert out["parameters"]["profile_points"] == [[0, 0], [40, 0], [40, 20], [0, 20]]
    assert out["confidence"] == 0.9

def test_revolve_angle_spelling_is_canonicalised():
    assert normalize_freeform({"revolve_angle": 90})["revolve_angle_deg"] == 90

@pytest.mark.parametrize("bad", ["nope", {"parameters": {}}, {"part_type": "x", "parameters": [1]},
                                 {"part_type": "x", "parameters": {}, "operations": "no"}])
def test_bad_top_level_shapes_are_rejected(bad):
    out, errs = validate_llm_response(bad, "")
    assert out is None and errs

def test_template_parts_pass_through_untouched():
    data = {"part_type": "shaft", "parameters": {"diameter_mm": 10, "length_mm": 50}, "confidence": 0.9}
    out, errs = validate_llm_response(data, "shaft 10mm diameter 50mm long")
    assert errs == [] and out["parameters"] == data["parameters"]


# ---- the worked examples inside the prompt must satisfy the gate ------------

def test_prompt_worked_examples_pass_the_validator():
    text = (ROOT / "deepseek_parser.py").read_text()
    examples = re.findall(r'^\d\. "(.+?)" ->\n(\{.+\})$', text, re.M)
    assert len(examples) >= 3
    for description, blob in examples:
        out, errs = validate_llm_response(json.loads(blob), description)
        assert errs == [], (description, errs)


# ---- which descriptions regex must never be trusted with --------------------

def test_needs_ai_parser():
    assert needs_ai_parser(SHARK)
    assert needs_ai_parser("A vase made by a revolve of a curved profile")
    assert not needs_ai_parser("shaft 10mm diameter, 50mm long")
    assert not needs_ai_parser("A flat plate 40mm long, no convex features")
