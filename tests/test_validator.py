"""
Unit tests for validator.py - pure Python arithmetic, no CadQuery needed,
so these run fast and everywhere (including CI without a native OCCT
build). Covers every part-type validator and the dispatch function.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from validator import (
    ValidationResult,
    validate_bearing,
    validate_connecting_rod,
    validate_crankshaft,
    validate_flat_plate,
    validate_gear,
    validate_l_bracket,
    validate_motor_mount,
    validate_parameters,
    validate_shaft,
    validate_simple_box,
)


class TestValidationResult:
    def test_starts_valid_with_no_errors(self):
        result = ValidationResult()
        assert result.is_valid
        assert result.errors == []
        assert result.warnings == []

    def test_add_error_flips_is_valid(self):
        result = ValidationResult()
        result.add_error("bad")
        assert not result.is_valid
        assert result.errors == ["bad"]

    def test_add_warning_does_not_flip_is_valid(self):
        result = ValidationResult()
        result.add_warning("hmm")
        assert result.is_valid

    def test_add_correction_records_old_and_new(self):
        result = ValidationResult()
        result.add_correction("thickness_mm", 10.0, 5.0)
        assert result.corrections["thickness_mm"] == {"old": 10.0, "new": 5.0}


class TestMotorMount:
    def test_valid_defaults_pass_clean(self):
        result = validate_motor_mount({})
        assert result.is_valid
        assert result.corrections == {}

    def test_oversized_hole_diameter_is_capped(self):
        result = validate_motor_mount({"hole_diameter_mm": 50.0})
        assert result.corrections["hole_diameter_mm"]["new"] == 20.0

    def test_fillet_too_large_for_thickness_is_reduced(self):
        result = validate_motor_mount({"thickness_mm": 3.0, "fillet_radius_mm": 5.0})
        assert result.corrections["fillet_radius_mm"]["new"] < 3.0 / 2

    def test_unusual_motor_size_falls_back_to_default(self):
        result = validate_motor_mount({"motor_size_mm": 5.0})
        assert result.corrections["motor_size_mm"]["new"] == 50.0


class TestLBracket:
    def test_negative_dimension_is_an_error_not_a_warning(self):
        result = validate_l_bracket({"width_mm": -10.0})
        assert not result.is_valid
        assert any("width" in e for e in result.errors)

    def test_hole_diameter_ge_thickness_is_corrected(self):
        result = validate_l_bracket({"thickness_mm": 3.0, "hole_diameter_mm": 5.0})
        assert result.corrections["hole_diameter_mm"]["new"] < 3.0

    def test_thickness_too_large_relative_to_body_is_reduced(self):
        result = validate_l_bracket(
            {"width_mm": 10.0, "height_mm": 10.0, "depth_mm": 10.0, "thickness_mm": 9.0}
        )
        assert "thickness_mm" in result.corrections


class TestFlatPlate:
    def test_all_nonpositive_dims_is_single_error(self):
        result = validate_flat_plate({"length_mm": 0, "width_mm": 0, "thickness_mm": 0})
        assert not result.is_valid

    def test_hole_count_below_one_is_corrected_up_to_one(self):
        result = validate_flat_plate({"hole_count_x": 0, "hole_count_y": -3})
        assert result.corrections["hole_count_x"]["new"] == 1
        assert result.corrections["hole_count_y"]["new"] == 1


class TestShaft:
    def test_zero_diameter_is_an_error(self):
        result = validate_shaft({"diameter_mm": 0, "length_mm": 50})
        assert not result.is_valid

    def test_length_shorter_than_diameter_is_corrected(self):
        result = validate_shaft({"diameter_mm": 30.0, "length_mm": 5.0})
        assert result.corrections["length_mm"]["new"] == 60.0


class TestGear:
    def test_few_teeth_warns_but_does_not_invalidate(self):
        result = validate_gear({"teeth": 5})
        assert result.is_valid
        assert any("undercut" in w for w in result.warnings)

    def test_nonpositive_module_is_an_error(self):
        result = validate_gear({"module": 0})
        assert not result.is_valid

    def test_oversized_bore_is_reduced(self):
        # pitch_diameter = module * teeth = 2 * 20 = 40; bore >= 32 triggers correction
        result = validate_gear({"module": 2.0, "teeth": 20, "bore_diameter_mm": 35.0})
        assert result.corrections["bore_diameter_mm"]["new"] == 20.0


class TestBearing:
    def test_outer_not_greater_than_inner_is_an_error_with_correction(self):
        result = validate_bearing({"inner_diameter_mm": 20.0, "outer_diameter_mm": 10.0})
        assert not result.is_valid
        assert result.corrections["outer_diameter_mm"]["new"] == 40.0

    def test_nonpositive_width_is_an_error(self):
        result = validate_bearing({"width_mm": -1})
        assert not result.is_valid


class TestSimpleBox:
    def test_wall_too_thick_for_box_is_reduced(self):
        result = validate_simple_box(
            {"length_mm": 20.0, "width_mm": 20.0, "height_mm": 20.0, "wall_thickness_mm": 15.0}
        )
        assert "wall_thickness_mm" in result.corrections
        assert result.corrections["wall_thickness_mm"]["new"] < 20.0 / 2


class TestConnectingRod:
    def test_valid_defaults_pass_clean(self):
        result = validate_connecting_rod({})
        assert result.is_valid

    def test_boss_smaller_than_its_own_bore_is_an_error_with_correction(self):
        result = validate_connecting_rod(
            {"big_end_diameter_mm": 24.0, "big_end_boss_diameter_mm": 20.0}
        )
        assert not result.is_valid
        assert result.corrections["big_end_boss_diameter_mm"]["new"] > 24.0

    def test_center_distance_too_short_for_boss_sizes_is_widened(self):
        result = validate_connecting_rod(
            {
                "center_distance_mm": 10.0,
                "big_end_boss_diameter_mm": 40.0,
                "small_end_boss_diameter_mm": 22.0,
            }
        )
        assert result.corrections["center_distance_mm"]["new"] >= (40.0 + 22.0) / 2

    def test_nonpositive_shank_width_is_an_error(self):
        result = validate_connecting_rod({"shank_width_mm": 0})
        assert not result.is_valid


class TestCrankshaft:
    def test_valid_defaults_pass_clean(self):
        result = validate_crankshaft({})
        assert result.is_valid

    def test_zero_throws_is_an_error(self):
        result = validate_crankshaft({"num_throws": 0})
        assert not result.is_valid

    def test_very_high_throw_count_warns_but_does_not_invalidate(self):
        result = validate_crankshaft({"num_throws": 16})
        assert result.is_valid
        assert any("unusually high" in w for w in result.warnings)

    def test_tiny_stroke_relative_to_journals_is_widened(self):
        result = validate_crankshaft(
            {"stroke_mm": 1.0, "main_journal_diameter_mm": 50.0, "rod_journal_diameter_mm": 45.0}
        )
        assert "stroke_mm" in result.corrections
        assert result.corrections["stroke_mm"]["new"] > 1.0

    def test_mismatched_phase_angle_count_warns(self):
        result = validate_crankshaft({"num_throws": 4, "phase_angles_deg": [0, 180]})
        assert result.is_valid  # warning only, not an error
        assert any("phase_angles_deg" in w for w in result.warnings)


class TestValidateParametersDispatch:
    def test_known_part_type_routes_to_its_validator(self):
        corrected, result = validate_parameters("shaft", {"diameter_mm": 0, "length_mm": 10})
        assert not result.is_valid

    def test_unknown_part_type_passes_through_unvalidated(self):
        params = {"anything": 1}
        corrected, result = validate_parameters("some_future_part_type", params)
        assert corrected == params
        assert result.is_valid
        assert result.errors == []

    def test_corrections_are_applied_to_returned_params_not_just_reported(self):
        corrected, result = validate_motor_mount_via_dispatch()
        assert corrected["hole_diameter_mm"] == 20.0


def validate_motor_mount_via_dispatch():
    return validate_parameters("motor_mount", {"hole_diameter_mm": 999.0})
