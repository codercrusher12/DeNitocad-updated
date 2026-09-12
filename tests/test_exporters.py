"""
Tests for exporters.py - STEP/STL/IGES/DXF/PDF export.

Same convention as test_cad_templates.py: marked `requires_cadquery` and
skipped automatically when cadquery/OCP isn't installed (see
pyproject.toml's markers and conftest.py). exporters.py imports cadquery
at module level, so importing the module at all requires the real
dependency - there's no meaningful "pure logic" subset of this file that
can run without it.

These were written against the cadquery 2.3.1 / OCP API surface pinned in
requirements.txt but not executed against real OCCT geometry (see the
scope note at the top of exporters.py) - run this file for real, in an
environment with the project's actual dependencies installed, before
shipping.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

cadquery = pytest.importorskip("cadquery", reason="requires a real CadQuery/OCCT install")
pytest.importorskip("ezdxf", reason="requires ezdxf for DXF export tests")
pytest.importorskip("reportlab", reason="requires reportlab for PDF export tests")

import exporters
from cad_templates import TEMPLATES
from exceptions import UnsupportedFormatError

pytestmark = pytest.mark.requires_cadquery


@pytest.fixture
def flat_plate_workplane():
    """flat_plate is the simplest template with holes - exactly the shape
    DXF/PDF section-based export is designed around (see the scope note
    in exporters.py)."""
    return TEMPLATES["flat_plate"](
        {
            "width_mm": 60.0,
            "length_mm": 40.0,
            "thickness_mm": 5.0,
            "hole_diameter_mm": 6.0,
            "hole_positions": [[10, 10], [50, 10], [10, 30], [50, 30]],
        }
    )


@pytest.fixture
def simple_box_workplane():
    return TEMPLATES["simple_box"]({"width_mm": 30.0, "length_mm": 20.0, "height_mm": 10.0})


class TestValidateFormats:
    def test_defaults_to_every_supported_format(self):
        # Multi-format export is the default as of 2.2.1 - an empty/omitted
        # `formats` list now produces every supported format, not just
        # step+stl. Order matches exporters.DEFAULT_FORMATS.
        assert exporters.validate_formats([]) == ["step", "stl", "iges", "dxf", "pdf"]

    def test_accepts_known_formats_case_insensitively(self):
        assert exporters.validate_formats(["STEP", "Dxf", "pdf"]) == ["step", "dxf", "pdf"]

    def test_deduplicates_preserving_order(self):
        assert exporters.validate_formats(["stl", "step", "stl"]) == ["stl", "step"]

    def test_rejects_unknown_format(self):
        with pytest.raises(UnsupportedFormatError):
            exporters.validate_formats(["stpe"])


class TestStepAndStl:
    def test_export_step_produces_a_file(self, simple_box_workplane, tmp_path):
        out = exporters.export_step(simple_box_workplane, tmp_path / "box.step")
        assert out.exists() and out.stat().st_size > 0

    def test_export_stl_produces_a_file(self, simple_box_workplane, tmp_path):
        out = exporters.export_stl(simple_box_workplane, tmp_path / "box.stl")
        assert out.exists() and out.stat().st_size > 0


class TestIges:
    def test_export_iges_produces_a_file(self, simple_box_workplane, tmp_path):
        out = exporters.export_iges(simple_box_workplane, tmp_path / "box.igs")
        assert out.exists() and out.stat().st_size > 0

    def test_export_iges_file_has_iges_start_record(self, simple_box_workplane, tmp_path):
        # Every valid IGES file begins its first 72-column "S" (Start)
        # section record at column 1 - cheap sanity check that this
        # isn't an empty/garbage file even without a full IGES parser.
        out = exporters.export_iges(simple_box_workplane, tmp_path / "box.igs")
        first_line = out.read_text(errors="replace").splitlines()[0]
        assert first_line[72:73] == "S"


class TestDxf:
    def test_export_dxf_produces_a_file_with_expected_layers(self, flat_plate_workplane, tmp_path):
        import ezdxf

        out = exporters.export_dxf(flat_plate_workplane, tmp_path / "plate.dxf", part_name="test_plate")
        assert out.exists()

        doc = ezdxf.readfile(str(out))
        layer_names = {layer.dxf.name for layer in doc.layers}
        # VISIBLE/HIDDEN replace the old PIECE_OUTLINE/HOLES/CENTERLINES
        # set - HLR classifies by visibility, not by section-wire nesting.
        assert {"VISIBLE", "HIDDEN", "LABELS", "BORDER"} <= layer_names

    def test_export_dxf_has_three_view_labels(self, flat_plate_workplane, tmp_path):
        import ezdxf

        out = exporters.export_dxf(flat_plate_workplane, tmp_path / "plate.dxf", part_name="test_plate")
        doc = ezdxf.readfile(str(out))
        msp = doc.modelspace()
        label_texts = {t.dxf.text for t in msp.query('TEXT[layer=="LABELS"]')}
        assert {"FRONT VIEW", "TOP VIEW", "RIGHT SIDE VIEW"} <= label_texts

    def test_export_dxf_has_visible_and_hidden_geometry(self, simple_box_workplane, tmp_path):
        import ezdxf

        out = exporters.export_dxf(simple_box_workplane, tmp_path / "box.dxf")
        doc = ezdxf.readfile(str(out))
        msp = doc.modelspace()
        # A box has edges in every view; visible-edge polylines should
        # exist regardless of orientation quirks. Hidden-edge presence
        # depends on view direction, so only visible is asserted here.
        assert len(list(msp.query('LWPOLYLINE[layer=="VISIBLE"]'))) > 0

    def test_export_dxf_multi_part_draws_one_block_per_part(
        self, flat_plate_workplane, simple_box_workplane, tmp_path
    ):
        import ezdxf

        parts = [
            ("base_plate", flat_plate_workplane.val()),
            ("box_bracket", simple_box_workplane.val()),
        ]
        out = exporters.export_dxf(
            flat_plate_workplane, tmp_path / "assembly.dxf", part_name="assembly", parts=parts
        )
        doc = ezdxf.readfile(str(out))
        msp = doc.modelspace()
        label_texts = {t.dxf.text for t in msp.query('TEXT[layer=="LABELS"]')}
        assert "BASE_PLATE" in label_texts
        assert "BOX_BRACKET" in label_texts
        # Each part contributes its own front/top/side labels on top of
        # its part-name label, so there should be well more than the
        # 3 view labels a single-part export would produce.
        assert len(list(msp.query('TEXT[layer=="LABELS"]'))) > 3 + 2


class TestPdf:
    def test_export_pdf_produces_a_valid_pdf_file(self, flat_plate_workplane, tmp_path):
        out = exporters.export_pdf(
            flat_plate_workplane, tmp_path / "plate.pdf", part_name="test_plate", material="Aluminum 6061"
        )
        assert out.exists()
        with open(out, "rb") as f:
            header = f.read(5)
        assert header == b"%PDF-"

    def test_export_pdf_multi_part_produces_a_valid_pdf_file(
        self, flat_plate_workplane, simple_box_workplane, tmp_path
    ):
        parts = [
            ("base_plate", flat_plate_workplane.val()),
            ("box_bracket", simple_box_workplane.val()),
        ]
        out = exporters.export_pdf(
            flat_plate_workplane, tmp_path / "assembly.pdf", part_name="assembly", parts=parts
        )
        assert out.exists()
        with open(out, "rb") as f:
            header = f.read(5)
        assert header == b"%PDF-"


class TestAssemblyParts:
    def test_get_assembly_parts_returns_one_entry_per_leaf_part(self):
        from assembly import generate_assembly, get_assembly_parts, parse_assembly_description

        parts_spec = parse_assembly_description("base plate with l bracket")
        assy = generate_assembly(parts_spec)
        parts = get_assembly_parts(assy)
        assert {name for name, _ in parts} == {"base_plate", "bracket"}
        for _, shape in parts:
            assert shape is not None


class TestExportAll:
    def test_export_all_default_formats(self, simple_box_workplane, tmp_path):
        # No `formats` passed -> every supported format, per DEFAULT_FORMATS.
        results = exporters.export_all(simple_box_workplane, tmp_path, "box")
        assert set(results.keys()) == {"step", "stl", "iges", "dxf", "pdf"}
        for p in results.values():
            assert p.exists()

    def test_export_all_every_supported_format(self, flat_plate_workplane, tmp_path):
        results = exporters.export_all(
            flat_plate_workplane,
            tmp_path,
            "plate",
            formats=list(exporters.SUPPORTED_FORMATS),
            part_name="plate",
            material="Steel",
            part_type="flat_plate",
        )
        assert set(results.keys()) == exporters.SUPPORTED_FORMATS
        for p in results.values():
            assert p.exists() and p.stat().st_size > 0

    def test_export_all_rejects_unknown_format(self, simple_box_workplane, tmp_path):
        with pytest.raises(UnsupportedFormatError):
            exporters.export_all(simple_box_workplane, tmp_path, "box", formats=["step", "not_a_format"])


class TestGenerateFromTextWithFormats:
    """End-to-end through CADGenerator, same style as
    test_cad_templates.py's full-pipeline test - confirms the formats
    param actually threads through cad_generator.py, not just the
    exporters module in isolation."""

    def test_generate_from_text_respects_requested_formats(self, tmp_path):
        from cad_generator import CADGenerator

        generator = CADGenerator(output_dir=str(tmp_path))
        result = generator.generate_from_text(
            "flat plate 60mm x 40mm x 5mm thick",
            use_deepseek=False,
            formats=["step", "dxf", "pdf"],
            user_id="test-user",
        )
        assert result["success"], result.get("error")
        assert result["step_file"] is not None
        assert result["dxf_file"] is not None
        assert result["pdf_file"] is not None
        assert result["stl_file"] is None  # not requested
        assert result["iges_file"] is None  # not requested
        assert Path(result["dxf_file"]).exists()
        assert Path(result["pdf_file"]).exists()

    def test_generate_from_text_rejects_bad_format_before_generating(self, tmp_path):
        from cad_generator import CADGenerator

        generator = CADGenerator(output_dir=str(tmp_path))
        result = generator.generate_from_text(
            "flat plate 60mm x 40mm x 5mm thick",
            use_deepseek=False,
            formats=["not_a_real_format"],
            user_id="test-user",
        )
        assert result["success"] is False
        assert result["error_type"] == "UnsupportedFormatError"
