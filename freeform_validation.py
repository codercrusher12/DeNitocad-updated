"""
Deterministic validation for LLM-produced freeform CAD parameters.

The LLM interprets the language; this module refuses parses that contradict
the description or that the geometry engine cannot build faithfully, before
anything reaches CadQuery.

It only *normalises* representation (drops a repeated closing point and exact
consecutive duplicates - neither changes the shape). It never invents or
moves coordinates. No third-party dependencies, so it is unit-testable
anywhere.

Bump VALIDATOR_VERSION whenever a rule changes: it is part of the parse-cache
key, so stale cached parses are not served under looser/older rules.
"""
from __future__ import annotations

import math
import re
from typing import Any

VALIDATOR_VERSION = "2"

FREEFORM_OPS = {"sweep", "loft", "revolve"}
MIN_CURVE_POINTS = 8
DIM_TOLERANCE = 0.10          # +/-10% on explicitly stated dimensions
MAX_COORD_MM = 10_000.0
_EPS = 1e-9

Pt2 = tuple[float, float]


# --------------------------------------------------------------------------
# Top-level response shape
# --------------------------------------------------------------------------

def validate_llm_response(
    data: Any, description: str = ""
) -> tuple[dict[str, Any] | None, list[str]]:
    """Validate the LLM JSON. Returns (clean_dict, []) or (None, errors)."""
    if not isinstance(data, dict):
        return None, ["LLM response must be a JSON object"]

    part_type = data.get("part_type")
    if not isinstance(part_type, str) or not part_type.strip():
        return None, ["LLM response is missing a string part_type"]

    parameters = data.get("parameters", {})
    if not isinstance(parameters, dict):
        return None, ["LLM response 'parameters' must be an object"]

    operations = data.get("operations") or []
    if not isinstance(operations, list) or not all(isinstance(o, dict) for o in operations):
        return None, ["LLM response 'operations' must be a list of objects"]

    material = data.get("material")
    if material is not None and not isinstance(material, str):
        return None, ["LLM response 'material' must be a string or null"]

    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    if not math.isfinite(confidence):
        confidence = 0.0
    confidence = min(1.0, max(0.0, confidence))

    if part_type == "freeform":
        parameters = normalize_freeform(parameters)
        errors = validate_freeform(parameters, description)
        if errors:
            return None, errors

    return {
        "part_type": part_type,
        "parameters": parameters,
        "material": material,
        "operations": operations,
        "confidence": confidence,
    }, []


# --------------------------------------------------------------------------
# Normalisation (representation only)
# --------------------------------------------------------------------------

def _clean_ring(raw: Any, dims: int) -> Any:
    """Drop exact consecutive duplicates and a repeated closing point."""
    if not isinstance(raw, list):
        return raw
    out: list[Any] = []
    for p in raw:
        if out and isinstance(p, (list, tuple)) and list(p) == list(out[-1]):
            continue
        out.append(list(p) if isinstance(p, (list, tuple)) else p)
    if len(out) > 1 and out[0] == out[-1]:
        out.pop()
    return out


def normalize_freeform(parameters: dict[str, Any]) -> dict[str, Any]:
    params = dict(parameters)
    if "profile_points" in params:
        params["profile_points"] = _clean_ring(params["profile_points"], 2)
    if isinstance(params.get("profiles"), list):
        cleaned = []
        for item in params["profiles"]:
            if isinstance(item, dict):
                item = dict(item)
                if "points" in item:
                    item["points"] = _clean_ring(item["points"], 2)
            cleaned.append(item)
        params["profiles"] = cleaned
    # One canonical spelling for the revolve angle.
    if "revolve_angle" in params and "revolve_angle_deg" not in params:
        params["revolve_angle_deg"] = params.pop("revolve_angle")
    return params


# --------------------------------------------------------------------------
# Geometry helpers
# --------------------------------------------------------------------------

def _points(raw: Any, dims: int) -> list[tuple[float, ...]]:
    if not isinstance(raw, list):
        return []
    out: list[tuple[float, ...]] = []
    for p in raw:
        if not isinstance(p, (list, tuple)) or len(p) != dims:
            return []
        try:
            vals = tuple(float(x) for x in p)
        except (TypeError, ValueError):
            return []
        if not all(math.isfinite(x) and abs(x) <= MAX_COORD_MM for x in vals):
            return []
        out.append(vals)
    return out


def _polygon_area(points: list[Pt2]) -> float:
    n = len(points)
    return abs(sum(
        points[i][0] * points[(i + 1) % n][1] - points[(i + 1) % n][0] * points[i][1]
        for i in range(n)
    )) / 2.0


def _orient(a: Pt2, b: Pt2, c: Pt2) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(a: Pt2, b: Pt2, p: Pt2) -> bool:
    return (min(a[0], b[0]) - _EPS <= p[0] <= max(a[0], b[0]) + _EPS
            and min(a[1], b[1]) - _EPS <= p[1] <= max(a[1], b[1]) + _EPS)


def _segments_intersect(a: Pt2, b: Pt2, c: Pt2, d: Pt2) -> bool:
    o1, o2, o3, o4 = _orient(a, b, c), _orient(a, b, d), _orient(c, d, a), _orient(c, d, b)
    if ((o1 > _EPS and o2 < -_EPS) or (o1 < -_EPS and o2 > _EPS)) and \
       ((o3 > _EPS and o4 < -_EPS) or (o3 < -_EPS and o4 > _EPS)):
        return True
    return ((abs(o1) <= _EPS and _on_segment(a, b, c))
            or (abs(o2) <= _EPS and _on_segment(a, b, d))
            or (abs(o3) <= _EPS and _on_segment(c, d, a))
            or (abs(o4) <= _EPS and _on_segment(c, d, b)))


def _self_intersects(points: list[Pt2]) -> bool:
    """True if any two non-adjacent edges of the closed polygon touch/cross."""
    n = len(points)
    for i in range(n):
        a, b = points[i], points[(i + 1) % n]
        for j in range(i + 1, n):
            if j == i or (j + 1) % n == i or (i + 1) % n == j:
                continue  # adjacent edges share a vertex by construction
            c, d = points[j], points[(j + 1) % n]
            if _segments_intersect(a, b, c, d):
                return True
    return False


def _has_concave_turn(points: list[Pt2]) -> bool:
    """A turn opposite the polygon's dominant winding (needs a simple polygon)."""
    signs: list[int] = []
    n = len(points)
    for i in range(n):
        cross = _orient(points[i - 1], points[i], points[(i + 1) % n])
        if abs(cross) > 1e-7:
            signs.append(1 if cross > 0 else -1)
    if len(signs) < 3:
        return False
    dominant = 1 if sum(signs) >= 0 else -1
    return any(s != dominant for s in signs)


# --------------------------------------------------------------------------
# Reading the description
# --------------------------------------------------------------------------

_NUM = r"(\d+(?:\.\d+)?)"
_DIM_WORDS = (
    r"(?:long|in\s+length|tall|high|in\s+height|wide|in\s+width|"
    r"at\s+the\s+base|diameter|in\s+diameter)"
)
_DIM_PATTERNS = (
    re.compile(rf"\b{_NUM}\s*mm\s+{_DIM_WORDS}\b"),
    re.compile(rf"\b(?:length|height|width|diameter)\s+(?:of\s+)?{_NUM}\s*mm\b"),
)
_NEGATION_RE = re.compile(r"\b(no|without|non|not|avoid|zero)\b[\s-]*(?:\w+\s+){0,2}$")


def _requested(text: str, words: tuple[str, ...]) -> bool:
    """True if any word appears and is not negated ('no convex features')."""
    for m in re.finditer(r"\b(" + "|".join(words) + r")\b", text):
        if not _NEGATION_RE.search(text[max(0, m.start() - 24):m.start()]):
            return True
    return False


_AI_ONLY_WORDS = ("curved", "curves", "curve", "curving", "concave", "convex", "organic",
                  "arc", "freeform", "free-form", "sweep", "swept", "loft", "lofted",
                  "revolve", "revolved")


def needs_ai_parser(description: str) -> bool:
    """True if the wording asks for geometry the rule-based parser cannot model
    (curves, custom outlines, sweeps/lofts/revolves). Regex would silently turn
    such a request into a plain template part."""
    return _requested(description.lower(), _AI_ONLY_WORDS)


def stated_dimensions(description: str) -> list[float]:
    """Every explicit overall dimension in mm ('50mm tall', 'diameter of 30 mm')."""
    text = description.lower()
    found: list[float] = []
    for pat in _DIM_PATTERNS:
        for m in pat.finditer(text):
            found.append(float(m.group(1)))
    return found


def _extents(parameters: dict[str, Any]) -> list[float] | None:
    """Approximate overall extents (mm) of the solid the parameters describe."""
    op = parameters.get("operation")
    if op in ("sweep", "revolve"):
        prof = _points(parameters.get("profile_points"), 2)
        if not prof:
            return None
        xs, ys = [p[0] for p in prof], [p[1] for p in prof]
        w, h = max(xs) - min(xs), max(ys) - min(ys)
        if op == "sweep":
            path = _points(parameters.get("path_points"), 3)
            if not path:
                return None
            px, pz = [p[0] for p in path], [p[2] for p in path]
            return [w + (max(px) - min(px)), h, max(pz) - min(pz)]
        axis = parameters.get("revolve_axis", "Y")
        if axis == "Y":
            return [2 * max(abs(x) for x in xs), h]
        return [2 * max(abs(y) for y in ys), w]
    if op == "loft":
        allx, ally, zs = [], [], []
        for item in parameters.get("profiles") or []:
            pts = _points(item.get("points"), 2) if isinstance(item, dict) else []
            if not pts:
                return None
            allx += [p[0] for p in pts]
            ally += [p[1] for p in pts]
            try:
                zs.append(float(item.get("z")))
            except (TypeError, ValueError):
                return None
        return [max(allx) - min(allx), max(ally) - min(ally), max(zs) - min(zs)]
    return None


# --------------------------------------------------------------------------
# Per-part checks
# --------------------------------------------------------------------------

def _check_ring(points: list[Pt2], label: str) -> list[str]:
    errs: list[str] = []
    if len(points) < 3:
        return [f"{label} needs at least 3 valid [x,y] points"]
    if _polygon_area(points) <= _EPS:
        errs.append(f"{label} forms a zero-area or degenerate profile")
    elif _self_intersects(points):
        errs.append(f"{label} is self-intersecting; trace the outline once, in order")
    return errs


def validate_freeform(parameters: dict[str, Any], description: str = "") -> list[str]:
    """Return fidelity/geometry errors for a freeform parse ([] means accepted)."""
    parameters = normalize_freeform(parameters)
    op = parameters.get("operation")
    if op not in FREEFORM_OPS:
        return ["freeform.operation must be one of sweep, loft, revolve"]

    errors: list[str] = []
    text = description.lower()
    curve_wanted = _requested(text, ("curved", "curves", "curve", "curving",
                                     "concave", "convex", "organic", "arc", "arced"))
    concave_wanted = _requested(text, ("concave",))
    profile: list[Pt2] = []
    profile_count = 0

    if op in ("sweep", "revolve"):
        profile = _points(parameters.get("profile_points"), 2)  # type: ignore[assignment]
        profile_count = len(profile)
        errors += _check_ring(profile, "freeform.profile_points")

    if op == "sweep":
        path = _points(parameters.get("path_points"), 3)
        if len(path) < 2:
            errors.append("sweep requires at least 2 valid [x,y,z] path points")
        else:
            if any(abs(p[1]) > _EPS for p in path):
                errors.append("sweep path_points must have y=0 for every point "
                              "([x,0,z]); 3D paths are not supported")
            length = sum(math.dist(path[i], path[i + 1]) for i in range(len(path) - 1))
            if length <= _EPS:
                errors.append("sweep path has zero length")

    elif op == "loft":
        profiles = parameters.get("profiles")
        if not isinstance(profiles, list) or len(profiles) < 2:
            errors.append("loft requires a 'profiles' list with at least 2 profiles "
                          "of the form {\"points\": [[x,y],...], \"z\": number}")
        else:
            zs: list[float] = []
            for i, item in enumerate(profiles):
                if not isinstance(item, dict):
                    errors.append(f"loft profile {i} must be an object")
                    continue
                pts = _points(item.get("points"), 2)
                profile_count = max(profile_count, len(pts))
                errors += _check_ring(pts, f"loft profile {i}")  # type: ignore[arg-type]
                try:
                    zs.append(float(item["z"]))
                except (KeyError, TypeError, ValueError):
                    errors.append(f"loft profile {i} needs a numeric z")
            if len(set(zs)) != len(zs):
                errors.append("loft profiles must have distinct z values")

    elif op == "revolve":
        try:
            angle = float(parameters.get("revolve_angle_deg", 360))
            if not 0 < angle <= 360:
                errors.append("revolve_angle_deg must be >0 and <=360")
        except (TypeError, ValueError):
            errors.append("revolve_angle_deg must be numeric")
        axis = parameters.get("revolve_axis", "Y")
        if axis not in ("X", "Y"):
            errors.append("revolve_axis must be X or Y")
        elif profile:
            idx = 0 if axis == "Y" else 1
            vals = [p[idx] for p in profile]
            if min(vals) < -1e-6 and max(vals) > 1e-6:
                errors.append(f"revolve profile crosses the {axis} axis; keep every "
                              f"{'x' if idx == 0 else 'y'} coordinate on one side of 0")

    # Fidelity rules that depend on the wording of the description.
    if curve_wanted and profile_count and profile_count < MIN_CURVE_POINTS:
        errors.append(
            f"description asks for a curved/concave/convex edge but the profile has only "
            f"{profile_count} points; use at least {MIN_CURVE_POINTS} distributed along the curve"
        )
    if concave_wanted and op in ("sweep", "revolve") and len(profile) >= 3 \
            and not _self_intersects(profile) and not _has_concave_turn(profile):
        errors.append("description asks for a concave edge but the parsed polygon is entirely convex")

    # Stated overall dimensions must appear among the solid's extents.
    if not errors:
        ext = _extents(parameters)
        if ext:
            for want in stated_dimensions(description):
                if not any((1 - DIM_TOLERANCE) * want <= e <= (1 + DIM_TOLERANCE) * want for e in ext):
                    got = ", ".join(f"{e:g}" for e in ext)
                    errors.append(
                        f"description states {want:g}mm but the parsed solid's extents are "
                        f"{got}mm (tolerance ±{int(DIM_TOLERANCE * 100)}%)"
                    )
    return errors
