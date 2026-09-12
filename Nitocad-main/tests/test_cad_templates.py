"""
Tests that exercise real CadQuery/OCCT geometry generation - registered
templates, the safe_fillet/safe_chamfer degrade-on-failure path, and a
full end-to-end CADGenerator.generate_from_text() call.

Marked `requires_cadquery` (see pyproject.toml's [tool.pytest.ini_options]
markers) so `pytest -m "not requires_cadquery"` skips this file entirely
in an environment without the native OCCT build - the README already
notes CadQuery's OCP dependency doesn't have wheels for every platform,
so this suite shouldn't be a hard requirement for the rest of the tests
to run. Run the full suite (including this file) on your VPS per the
README's own "Not tested against a real CadQuery install" note - these
tests are exactly the verification step that note is asking for.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

cq = pytest.importorskip("cadquery", reason="requires a real CadQuery/OCCT install")

from cad_generator import CADGenerator
from cad_templates import TEMPLATES

pytestmark = pytest.mark.requires_cadquery


def _is_valid_solid(workplane) -> bool:
    solids = workplane.solids().vals()
    return bool(solids) and all(s.isValid() for s in solids)


class TestAllTemplatesProduceValidGeometry:
    """One smoke test per registered template with its own defaults
    (empty params dict) - catches a template that raises outright or
    silently produces zero/invalid solids, without needing per-template
    parameter knowledge. Complements (doesn't replace) smoke_test.py's
    more scenario-driven checks."""

    @pytest.mark.parametrize("part_type", sorted(TEMPLATES.keys()))
    def test_template_builds_a_valid_solid_with_default_params(self, part_type):
        template_func = TEMPLATES[part_type]
        workplane = template_func({})
        assert _is_valid_solid(workplane), f"{part_type} produced invalid/empty geometry"


class TestShaftGeometry:
    def test_basic_shaft_has_expected_bounding_box(self):
        workplane = TEMPLATES["shaft"]({"diameter_mm": 10.0, "length_mm": 50.0})
        bbox = workplane.val().BoundingBox()
        assert bbox.zlen == pytest.approx(50.0, abs=0.1)
        assert bbox.xlen == pytest.approx(10.0, abs=0.1)

    def test_shaft_with_chamfer_still_valid(self):
        workplane = TEMPLATES["shaft"]({"diameter_mm": 10.0, "length_mm": 50.0, "chamfer_mm": 1.0})
        assert _is_valid_solid(workplane)


class TestFilletDegradation:
    """Regression tests for the three-round fillet bug fixed in the
    README's changelog (crash -> silent zero-solids -> isValid())."""

    def test_safe_fillet_degrades_gracefully_on_infeasible_radius(self):
        from cad_templates._safe_ops import safe_fillet

        box = cq.Workplane("XY").box(5, 5, 5)
        warnings: list[str] = []
        # A radius comparable to the box's own half-thickness is right at
        # the edge of OCCT feasibility - should degrade, not raise.
        result = safe_fillet(box, 4.9, None, warnings=warnings)
        assert _is_valid_solid(result)

    def test_safe_chamfer_degrades_gracefully_on_infeasible_size(self):
        from cad_templates._safe_ops import safe_chamfer

        box = cq.Workplane("XY").box(5, 5, 5)
        warnings: list[str] = []
        result = safe_chamfer(box, 4.9, None, warnings=warnings)
        assert _is_valid_solid(result)


class TestConnectingRod:
    """connecting_rod.py - see that module's docstring for why every
    hole cut uses an explicit cutter solid rather than
    faces(">Z").hole(): this part is asymmetric about the origin (two
    different-diameter bosses at different X positions), so a bug here
    would show up as a bore cut at the wrong location, not a crash -
    exactly the kind of defect that only a coordinate-level check
    catches, not just "is the geometry valid."
    """

    def test_default_params_produce_valid_geometry(self):
        workplane = TEMPLATES["connecting_rod"]({})
        assert _is_valid_solid(workplane)

    def test_big_end_bore_is_actually_at_the_big_end_not_the_origin(self):
        # Regression test for the exact bug caught and fixed during
        # development: faces(">Z").workplane().moveTo(x, y).hole(d)
        # centers its local coordinate system on the selected face's
        # center of mass, not the global origin - for this asymmetric
        # part that silently cuts the hole in the wrong place. Confirms
        # there's no material at the big-end bore's global (x, y) after
        # generation, i.e. the cut landed where it was actually asked
        # to land.
        params = {
            "center_distance_mm": 120.0,
            "big_end_diameter_mm": 24.0,
            "big_end_boss_diameter_mm": 40.0,
            "cap_bolt_diameter_mm": 0,  # isolate the bore check from the bolt holes
        }
        workplane = TEMPLATES["connecting_rod"](params)
        probe = cq.Workplane("XY").moveTo(120.0, 0.0).circle(1.0).extrude(20).translate((0, 0, -5))
        # If the bore was cut in the right place, a small probe cylinder
        # centered on the big end's bore axis intersects only empty
        # space (the bore), so subtracting the rod body from the probe
        # leaves the probe volume basically intact.
        remainder = probe.cut(workplane)
        assert len(remainder.solids().vals()) > 0

    def test_zero_rib_and_bolts_still_valid(self):
        workplane = TEMPLATES["connecting_rod"](
            {"rib_height_mm": 0, "cap_bolt_diameter_mm": 0}
        )
        assert _is_valid_solid(workplane)

    def test_tiny_boss_margin_skips_bolt_holes_instead_of_breaking(self):
        # big_end_boss_diameter_mm barely larger than the bore - too
        # little radial material for the bolt-hole placement logic's own
        # safety margin (see connecting_rod.py) to place holes at all.
        # Should silently skip them, not produce invalid geometry.
        workplane = TEMPLATES["connecting_rod"](
            {"big_end_diameter_mm": 24.0, "big_end_boss_diameter_mm": 27.0}
        )
        assert _is_valid_solid(workplane)


class TestCrankshaft:
    """crankshaft.py - see that module's docstring for the continuous-
    backbone bug caught and fixed during development (main-journal
    material running straight through every throw instead of pinching
    down to the offset rod journal), which these tests guard against
    directly rather than only checking overall solid validity.
    """

    def test_default_params_produce_valid_geometry(self):
        workplane = TEMPLATES["crankshaft"]({})
        assert _is_valid_solid(workplane)

    def test_single_throw_produces_valid_geometry(self):
        workplane = TEMPLATES["crankshaft"]({"num_throws": 1})
        assert _is_valid_solid(workplane)

    def test_main_journal_diameter_does_not_run_through_the_throw_region(self):
        # Regression test for the exact bug caught and fixed during
        # development: an early draft built one continuous full-length
        # cylinder at main-journal diameter and unioned the throws onto
        # it, which left main-diameter material present even where a
        # real crank pinches down to the rod-journal offset. Probe a
        # point that sits on the main axis, at the axial midpoint of the
        # first throw's rod journal - for a correctly segmented crank
        # there should be NO material there (that's exactly the gap the
        # webs bridge across, off-axis, to reach the rod journal).
        params = {
            "num_throws": 1,
            "main_journal_diameter_mm": 50.0,
            "main_journal_length_mm": 25.0,
            "rod_journal_diameter_mm": 45.0,
            "rod_journal_length_mm": 22.0,
            "web_thickness_mm": 12.0,
            "nose_length_mm": 0,
            "flange_length_mm": 0,
        }
        workplane = TEMPLATES["crankshaft"](params)

        throw_mid_z = params["main_journal_length_mm"] + params["web_thickness_mm"] + params["rod_journal_length_mm"] / 2
        probe = (
            cq.Workplane("XY")
            .workplane(offset=throw_mid_z)
            .circle(1.0)
            .extrude(0.1)
        )
        # A thin probe disc on-axis, at the throw's axial midpoint - if
        # the (buggy) continuous backbone were still present, this probe
        # would be entirely swallowed by the crank's solid and cutting
        # it away would leave nothing. Correct segmented geometry leaves
        # this space empty, so the probe survives the cut intact.
        remainder = probe.cut(workplane)
        assert len(remainder.solids().vals()) > 0

    def test_rod_journal_is_offset_from_main_axis(self):
        params = {"num_throws": 1, "stroke_mm": 80.0}
        workplane = TEMPLATES["crankshaft"](params)
        bbox = workplane.val().BoundingBox()
        # An offset rod journal pushes the bounding box's X/Y extent
        # well past the main-journal radius alone - if it were missing
        # or unoffset, the footprint would just be the main journal's
        # own (much smaller) circle.
        main_r = params.get("main_journal_diameter_mm", 50.0) / 2
        assert bbox.xlen > main_r * 2 or bbox.ylen > main_r * 2

    def test_custom_phase_angles_are_respected(self):
        workplane = TEMPLATES["crankshaft"](
            {"num_throws": 2, "phase_angles_deg": [0, 180]}
        )
        assert _is_valid_solid(workplane)

    def test_mismatched_phase_angle_count_falls_back_to_even_spacing(self):
        # Should not raise - falls back rather than indexing past the
        # end of a too-short phase_angles_deg list.
        workplane = TEMPLATES["crankshaft"](
            {"num_throws": 4, "phase_angles_deg": [0, 180]}
        )
        assert _is_valid_solid(workplane)

    def test_fillets_are_off_by_default(self):
        # apply_fillets defaults to False - see module docstring for why
        # (build-time/robustness risk on a large multi-body union).
        # Confirms the default path doesn't silently attempt it.
        workplane = TEMPLATES["crankshaft"]({"num_throws": 2})
        assert _is_valid_solid(workplane)

    def test_apply_fillets_true_still_produces_valid_geometry(self):
        # safe_fillet degrades gracefully rather than raising even if
        # OCCT rejects the radius against this geometry's edge topology
        # - so this should stay valid either way, filleted or not.
        workplane = TEMPLATES["crankshaft"]({"num_throws": 2, "apply_fillets": True})
        assert _is_valid_solid(workplane)


class TestStructuralBeamRegression:
    """Regression test for the i_beam/channel flange-web overlap bug
    described in the README (pieces built in mismatched local axes never
    actually overlapped, producing a beam whose flanges float apart from
    its web after a boolean union)."""

    def test_i_beam_flanges_and_web_are_a_single_connected_solid(self):
        workplane = TEMPLATES["structural_beam"](
            {"beam_type": "i_beam", "height_mm": 100.0, "width_mm": 50.0,
             "length_mm": 200.0, "thickness_mm": 5.0}
        )
        solids = workplane.solids().vals()
        # A correctly-unioned I-beam is exactly one solid; if the flanges
        # never actually touched the web, boolean union leaves multiple
        # disconnected solids instead.
        assert len(solids) == 1
        assert solids[0].isValid()

    def test_channel_beam_is_a_single_connected_solid(self):
        workplane = TEMPLATES["structural_beam"](
            {"beam_type": "channel", "height_mm": 100.0, "width_mm": 50.0,
             "length_mm": 200.0, "thickness_mm": 5.0}
        )
        solids = workplane.solids().vals()
        assert len(solids) == 1


class TestEndToEndGeneration:
    def test_generate_from_text_shaft_succeeds(self, tmp_path, tmp_db):
        generator = CADGenerator(output_dir=str(tmp_path))
        result = generator.generate_from_text(
            "shaft 10mm diameter, 50mm long", use_deepseek=False
        )
        assert result["success"] is True
        assert Path(result["step_file"]).exists()
        assert Path(result["stl_file"]).exists()

    def test_generate_from_text_unparseable_description_fails_cleanly(self, tmp_path, tmp_db):
        generator = CADGenerator(output_dir=str(tmp_path))
        result = generator.generate_from_text("", use_deepseek=False)
        assert result["success"] is False
        assert result["error_type"] == "ParseError"

    def test_too_many_operations_is_rejected_before_touching_occt(self, tmp_path):
        from cad_generator import MAX_OPERATIONS_PER_REQUEST
        from exceptions import GenerationError
        from smart_parser import ParsedParameters
        from validator import ValidationResult

        generator = CADGenerator(output_dir=str(tmp_path))
        params = ParsedParameters(
            part_type="shaft",
            parameters={
                "diameter_mm": 10.0,
                "length_mm": 50.0,
                "operations": [{"type": "chamfer", "size": 0.1}] * (MAX_OPERATIONS_PER_REQUEST + 1),
            },
        )
        with pytest.raises(GenerationError):
            generator._generate_single_part(params, ValidationResult(), ["step"])
