"""
Unit tests for smart_parser.py's regex/keyword parsing - the fallback
path used whenever DeepSeek isn't configured or fails. These exercise
real README example descriptions so a regression here is caught against
the same inputs the project advertises as working.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from smart_parser import infer_part_type, parse_description


class TestPartTypeInference:
    @pytest.mark.parametrize(
        "description,expected",
        [
            ("mounting bracket for a 50mm stepper motor, 4 holes", "motor_mount"),
            ("L-bracket 50mm wide, 60mm tall, 40mm deep", "l_bracket"),
            ("shaft 10mm diameter, 50mm long", "shaft"),
            ("gear with 20 teeth, module 2", "gear"),
            ("pulley 40mm outer, 10mm belt width", "pulley"),
            ("bearing 10mm inner, 20mm outer", "bearing"),
            ("box enclosure 100x80x50mm, 3mm walls", "simple_box"),
            ("I-beam 100mm tall, 50mm wide, 200mm long", "structural_beam"),
            ("pipe 20mm outer, 2mm wall, 50mm long", "pipe_fitting"),
            ("flange 100mm outer, 50mm bore, 10mm thick", "flange"),
            ("hex standoff for PCB mounting", "hex_standoff"),
            ("t-bracket for shelf support", "t_bracket"),
            ("cable channel bracket 50mm", "channel_bracket"),
            ("assemble multiple parts into one unit", "assembly"),
            ("connecting rod 120mm center distance", "connecting_rod"),
            ("conrod for a 4 cylinder engine", "connecting_rod"),
            ("crankshaft with 4 throws, 80mm stroke", "crankshaft"),
            ("crank shaft for inline 4 engine", "crankshaft"),
        ],
    )
    def test_keyword_routes_to_expected_part_type(self, description, expected):
        assert infer_part_type(description, [], []) == expected

    def test_crankshaft_is_not_shadowed_by_shaft_keyword(self):
        # "crankshaft" contains the substring "shaft" - must resolve to
        # the more specific crankshaft branch, not the generic shaft one
        # that would otherwise match first via plain substring search.
        assert infer_part_type("crankshaft, 6 throws", [], []) == "crankshaft"

    def test_connecting_rod_is_not_shadowed_by_rod_keyword(self):
        # "connecting rod" contains "rod", which is in the generic
        # shaft keyword list - same shadowing concern as above.
        assert infer_part_type("connecting rod, 100mm", [], []) == "connecting_rod"

    def test_specific_phrase_beats_generic_shadowing_keyword(self):
        # "hex standoff" contains "standoff" (-> spacer keyword list) but
        # must resolve to hex_standoff since that's checked first - this
        # is the exact ordering bug noted in the README's changelog.
        assert infer_part_type("hex standoff 5mm", [], []) == "hex_standoff"
        assert infer_part_type("channel bracket for cable routing", [], []) != "structural_beam"

    def test_no_keywords_falls_back_by_dimension_count(self):
        assert infer_part_type("something 10mm", [], []) == "flat_plate"


class TestParseDescription:
    def test_motor_mount_example_extracts_expected_shape(self):
        result = parse_description(
            "mounting bracket for a 50mm stepper motor, 4 holes, 5mm fillets, 3mm thick"
        )
        assert result.part_type == "motor_mount"
        assert isinstance(result.parameters, dict)
        assert result.confidence >= 0.0

    def test_shaft_dimensions_are_extracted_in_mm(self):
        result = parse_description("shaft 10mm diameter, 50mm long")
        assert result.part_type == "shaft"
        # Exact key names are an internal contract with validator.py /
        # cad_templates/primitives.py - if this drifts, both of those
        # silently stop receiving the value the description asked for.
        assert result.parameters.get("diameter_mm") == pytest.approx(10.0)
        assert result.parameters.get("length_mm") == pytest.approx(50.0)

    def test_inches_are_converted_to_millimeters(self):
        result = parse_description("shaft 1 inch diameter, 2 inches long")
        assert result.parameters.get("diameter_mm") == pytest.approx(25.4, rel=0.01)

    def test_empty_description_does_not_crash(self):
        result = parse_description("")
        assert result.part_type in {"flat_plate", "l_bracket", "unknown"}

    def test_gibberish_still_returns_a_parsedparameters_object(self):
        # The parser has no explicit "unknown" path for nonsense text (it
        # falls back to dimension-count heuristics), so this documents
        # actual current behavior rather than asserting failure.
        result = parse_description("asdkjfh qwoeiruqwoeiru")
        assert result.part_type
        assert result.parameters is not None
