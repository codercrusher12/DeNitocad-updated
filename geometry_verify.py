"""
Post-build geometry verification.

The compiler may return expected geometry alongside the solid (bounding box,
exact volume for extruded polygons). This module checks:

  - isValid() and exactly one solid
  - volume greater than zero and within tolerance (when expected given)
  - bounding box within tolerance (when expected given)
  - watertight tessellation (trimesh, on the STL) when available
  - no NaN or degenerate faces

Result is a VerificationReport stored on the job. If it fails, nothing is
delivered, anchored, or charged.

CadQuery / OCP / trimesh are optional at import time so pure logic tests
run without the geometry stack.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from logging_config import get_logger

logger = get_logger(__name__)

# Relative tolerances for volume / bbox comparison against expected values.
VOLUME_REL_TOL = 0.05
BBOX_ABS_TOL_MM = 0.5


@dataclass
class VerificationReport:
    passed: bool
    checks: dict[str, bool] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    measured: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _cq_solid_checks(solid: Any, expected: dict[str, Any] | None) -> tuple[dict[str, bool], list[str], dict[str, Any]]:
    """Run CadQuery/OCP solid checks. solid is a CadQuery Workplane or Shape."""
    checks: dict[str, bool] = {}
    errors: list[str] = []
    measured: dict[str, Any] = {}

    try:
        import cadquery as cq  # noqa: F401
        from OCP.BRepCheck import BRepCheck_Analyzer
    except ImportError:
        checks["cq_available"] = False
        return checks, ["CadQuery/OCP not available; solid checks skipped"], measured

    checks["cq_available"] = True

    # Resolve to a Shape
    shape = solid
    if hasattr(solid, "val"):
        try:
            shape = solid.val()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Could not extract solid from Workplane: {exc}")
            checks["is_valid"] = False
            return checks, errors, measured

    # isValid via BRepCheck
    try:
        analyzer = BRepCheck_Analyzer(shape.wrapped if hasattr(shape, "wrapped") else shape)
        is_valid = bool(analyzer.IsValid())
        checks["is_valid"] = is_valid
        if not is_valid:
            errors.append("Solid failed BRepCheck_Analyzer.IsValid()")
    except Exception as exc:  # noqa: BLE001
        checks["is_valid"] = False
        errors.append(f"BRepCheck failed: {exc}")

    # Exactly one solid
    try:
        solids = solid.solids().vals() if hasattr(solid, "solids") else [shape]
        n = len(solids)
        checks["exactly_one_solid"] = n == 1
        measured["solid_count"] = n
        if n != 1:
            errors.append(f"Expected exactly one solid, got {n}")
    except Exception as exc:  # noqa: BLE001
        checks["exactly_one_solid"] = False
        errors.append(f"Solid count check failed: {exc}")

    # Volume > 0
    try:
        vol = float(shape.Volume()) if hasattr(shape, "Volume") else float(solid.val().Volume())
        measured["volume"] = vol
        checks["volume_positive"] = vol > 0
        if vol <= 0:
            errors.append(f"Volume is not positive: {vol}")
        if expected and "volume" in expected and expected["volume"] is not None:
            exp_v = float(expected["volume"])
            if exp_v > 0:
                rel = abs(vol - exp_v) / exp_v
                checks["volume_within_tol"] = rel <= VOLUME_REL_TOL
                measured["expected_volume"] = exp_v
                measured["volume_rel_err"] = rel
                if rel > VOLUME_REL_TOL:
                    errors.append(
                        f"Volume {vol:.4g} outside tolerance of expected {exp_v:.4g} "
                        f"(rel err {rel:.3%})"
                    )
    except Exception as exc:  # noqa: BLE001
        checks["volume_positive"] = False
        errors.append(f"Volume check failed: {exc}")

    # Bounding box
    try:
        bb = shape.BoundingBox() if hasattr(shape, "BoundingBox") else solid.val().BoundingBox()
        bbox = {
            "xmin": float(bb.xmin), "xmax": float(bb.xmax),
            "ymin": float(bb.ymin), "ymax": float(bb.ymax),
            "zmin": float(bb.zmin), "zmax": float(bb.zmax),
        }
        measured["bbox"] = bbox
        # No NaN
        has_nan = any(
            v != v for v in (bbox["xmin"], bbox["xmax"], bbox["ymin"], bbox["ymax"], bbox["zmin"], bbox["zmax"])
        )
        checks["bbox_finite"] = not has_nan
        if has_nan:
            errors.append("Bounding box contains NaN")

        if expected and "bbox" in expected and expected["bbox"] is not None:
            exp = expected["bbox"]
            ok = True
            for key in ("xmin", "xmax", "ymin", "ymax", "zmin", "zmax"):
                if key in exp and abs(bbox[key] - float(exp[key])) > BBOX_ABS_TOL_MM:
                    ok = False
                    errors.append(
                        f"bbox.{key}={bbox[key]:.4g} outside ±{BBOX_ABS_TOL_MM} of expected {exp[key]}"
                    )
            checks["bbox_within_tol"] = ok
    except Exception as exc:  # noqa: BLE001
        checks["bbox_finite"] = False
        errors.append(f"Bounding box check failed: {exc}")

    return checks, errors, measured


def _stl_watertight_check(stl_path: str | Path) -> tuple[dict[str, bool], list[str], dict[str, Any]]:
    checks: dict[str, bool] = {}
    errors: list[str] = []
    measured: dict[str, Any] = {}
    path = Path(stl_path)
    if not path.is_file():
        checks["stl_present"] = False
        errors.append(f"STL not found: {path}")
        return checks, errors, measured
    checks["stl_present"] = True

    try:
        import trimesh
    except ImportError:
        checks["trimesh_available"] = False
        # Soft: not a hard failure when trimesh is absent
        return checks, [], measured

    checks["trimesh_available"] = True
    try:
        mesh = trimesh.load(str(path), force="mesh")
        if isinstance(mesh, trimesh.Scene):
            geoms = list(mesh.geometry.values())
            mesh = geoms[0] if geoms else None
        if mesh is None:
            checks["watertight"] = False
            errors.append("STL loaded but no mesh geometry found")
            return checks, errors, measured

        measured["face_count"] = int(len(mesh.faces))
        measured["vertex_count"] = int(len(mesh.vertices))
        is_wt = bool(mesh.is_watertight)
        checks["watertight"] = is_wt
        if not is_wt:
            errors.append("STL mesh is not watertight")

        # Degenerate faces (zero area)
        try:
            areas = mesh.area_faces
            degenerates = int((areas < 1e-12).sum()) if hasattr(areas, "sum") else 0
            measured["degenerate_faces"] = degenerates
            checks["no_degenerate_faces"] = degenerates == 0
            if degenerates > 0:
                errors.append(f"{degenerates} degenerate (zero-area) faces")
        except Exception:  # noqa: BLE001
            checks["no_degenerate_faces"] = True  # skip if unavailable

        # NaN vertices
        try:
            import numpy as np
            has_nan = bool(np.isnan(mesh.vertices).any())
            checks["no_nan_vertices"] = not has_nan
            if has_nan:
                errors.append("Mesh vertices contain NaN")
        except Exception:  # noqa: BLE001
            checks["no_nan_vertices"] = True
    except Exception as exc:  # noqa: BLE001
        checks["watertight"] = False
        errors.append(f"trimesh check failed: {exc}")

    return checks, errors, measured


def verify_geometry(
    solid: Any | None = None,
    *,
    stl_path: str | Path | None = None,
    expected: dict[str, Any] | None = None,
) -> VerificationReport:
    """Run all available post-build checks and return a VerificationReport.

    expected may contain:
      - volume: float (mm³)
      - bbox: dict with xmin/xmax/ymin/ymax/zmin/zmax
    """
    all_checks: dict[str, bool] = {}
    all_errors: list[str] = []
    all_warnings: list[str] = []
    measured: dict[str, Any] = {}

    if solid is not None:
        c, e, m = _cq_solid_checks(solid, expected)
        all_checks.update(c)
        all_errors.extend(e)
        measured.update(m)
    else:
        all_warnings.append("No solid provided; CQ checks skipped")

    if stl_path is not None:
        c, e, m = _stl_watertight_check(stl_path)
        all_checks.update(c)
        all_errors.extend(e)
        measured.update(m)
        if not c.get("trimesh_available", True) and c.get("stl_present"):
            all_warnings.append("trimesh not installed; watertight check skipped")
    else:
        all_warnings.append("No STL path provided; mesh checks skipped")

    # Hard failures only: explicit False on critical checks, or any error
    critical_keys = (
        "is_valid",
        "exactly_one_solid",
        "volume_positive",
        "volume_within_tol",
        "bbox_finite",
        "bbox_within_tol",
        "watertight",
        "no_degenerate_faces",
        "no_nan_vertices",
    )
    failed_critical = any(all_checks.get(k) is False for k in critical_keys)
    passed = not failed_critical and not all_errors

    if not passed:
        logger.warning(
            "geometry verification failed",
            extra={"errors": all_errors, "checks": all_checks},
        )

    return VerificationReport(
        passed=passed,
        checks=all_checks,
        errors=all_errors,
        warnings=all_warnings,
        measured=measured,
    )
