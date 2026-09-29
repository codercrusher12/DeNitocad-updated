"""
DeepSeek-based parameter extraction from natural language descriptions.

This restores the actual "LLM as brain" architecture that smart_parser.py
quietly abandoned, smart_parser.py is pure regex/keyword matching, no
language model involved at all, despite the project's own stated design
principle (LLM parses intent, deterministic templates do the geometry).
This module is real LLM parsing; the templates and validator remain
exactly as before, untouched, still doing all the actual geometry work.

UNVERIFIED IN THIS ENVIRONMENT: no internet access and no DeepSeek API
key available here, so this has not been run against the real API. It's
written against DeepSeek's current, documented OpenAI-compatible API
(base_url="https://api.deepseek.com", model="deepseek-v4-flash" or
"deepseek-v4-pro", current as of this build, DeepSeek has changed model
names before, see the note in api-docs.deepseek.com if this stops
working). Test with a real key before trusting this in production.
"""

import hashlib
import json
import re

from config import settings
from logging_config import get_logger
from smart_parser import ParsedParameters, parse_description as regex_fallback_parse
from exceptions import ParseError
from freeform_validation import VALIDATOR_VERSION, needs_ai_parser, validate_llm_response
import db

logger = get_logger(__name__)


PROMPT_VERSION = "freeform-fidelity-v3"

SYSTEM_PROMPT = """You are a CAD parameter extraction system. Parse natural language descriptions of mechanical parts into structured JSON.

Choose exactly one part_type from this list, and extract only the parameters relevant to it:

- motor_mount: motor_size_mm, thickness_mm, hole_diameter_mm, fillet_radius_mm
- l_bracket: width_mm, height_mm, depth_mm, thickness_mm, hole_count, hole_diameter_mm, fillet_radius_mm
- flat_plate: length_mm, width_mm, thickness_mm, hole_pattern ("rectangular"|"circular"), hole_count_x, hole_count_y, hole_diameter_mm, corner_fillet_mm
- simple_box: length_mm, width_mm, height_mm, wall_thickness_mm, has_lid (bool), lid_fit_tolerance_mm
- shaft: diameter_mm, length_mm, chamfer_mm, keyway_width_mm, keyway_depth_mm
- bearing: inner_diameter_mm, outer_diameter_mm, width_mm
- spacer: outer_diameter_mm, inner_diameter_mm, length_mm
- washer: outer_diameter_mm, inner_diameter_mm, thickness_mm
- sphere: diameter_mm
- gear: module, teeth, thickness_mm, bore_diameter_mm, pressure_angle
- pulley: outer_diameter_mm, belt_width_mm, bore_diameter_mm, thickness_mm, groove_depth_mm
- sprocket: teeth, chain_pitch_mm, thickness_mm, bore_diameter_mm
- structural_beam: beam_type ("i_beam"|"channel"), height_mm, width_mm, length_mm, thickness_mm
- angle: leg1_mm, leg2_mm, thickness_mm, length_mm
- tube: outer_diameter_mm, wall_thickness_mm, length_mm, shape ("round"|"square"|"rectangular")
- pipe_fitting: fitting_type ("pipe"|"elbow"|"tee"), outer_diameter_mm, wall_thickness_mm, length_mm, angle_deg
- flange: outer_diameter_mm, inner_diameter_mm, thickness_mm, hole_count, hole_diameter_mm, bolt_circle_mm
- hinge: length_mm, width_mm, thickness_mm, knuckle_count, pin_diameter_mm
- cam: base_radius_mm, lift_mm, thickness_mm, bore_diameter_mm, lift_profile ("simple"|"harmonic"|"cycloidal")
- hex_standoff: across_flats_mm, inner_diameter_mm, length_mm
- t_bracket: length_mm, cap_width_mm, stem_height_mm, thickness_mm, hole_count, hole_diameter_mm, fillet_radius_mm
- channel_bracket: length_mm, width_mm, height_mm, wall_thickness_mm, mount_hole_count, mount_hole_diameter_mm
- connecting_rod: center_distance_mm, big_end_diameter_mm, small_end_diameter_mm, thickness_mm
- crankshaft: num_throws, stroke_mm, main_journal_diameter_mm, main_journal_length_mm, rod_journal_diameter_mm, rod_journal_length_mm, web_thickness_mm, nose_diameter_mm, nose_length_mm, flange_diameter_mm, flange_length_mm
- freeform: for anything irregular/organic/custom-profile that doesn't match a template above. operation ("revolve"|"sweep"|"loft"), profile_points (list of [x,y] mm pairs tracing the 2D cross-section/profile in order - this is the actual shape), revolve_axis ("X"|"Y", for revolve), revolve_angle_deg (default 360, for revolve), path_points (list of [x,y,z] mm points, for sweep), profiles (list of {"points":[[x,y],...],"z":number}, for loft)

FREEFORM FIDELITY RULES (requirements, not suggestions):
- All coordinates are in millimetres. Preserve every stated dimension and geometric relationship exactly; never replace a described curved or concave edge with a straight shortcut or a triangle.
- Trace the outline ONCE, in order, as a simple closed polygon: no self-intersections, and do NOT repeat the first point at the end (the shape closes automatically).
- Any curved, concave, convex or organic edge MUST be sampled with at least 8 profile points distributed along the whole outline, dense where the curvature is strongest.
- Preserve concave versus convex. An edge that "curves inward" or is "concave" must produce at least one turn opposite to the polygon's overall winding; an entirely convex polygon is wrong.
- Sharp corners stay sharp; do not invent fillets, holes or dimensions that were not stated.
- sweep: the profile lies in the XY plane. path_points are [x, 0, z] and y MUST be 0. A flat plate of thickness t is a path [[0,0,0],[0,0,t]].
- loft: return at least two entries in `profiles`, each {"points": [[x,y],...], "z": number}, with distinct z. Do not use profile_points for a loft.
- revolve: profile_points lie on one side of the axis (all x >= 0 for axis "Y"). x is the RADIUS, so a 30mm diameter needs x = 15. Use `revolve_angle_deg`.

Worked examples (illustrative shapes, not to be copied):
1. "Tapered bracket arm, 60mm long and 20mm tall, top edge concave, 4mm thick" ->
{"part_type":"freeform","parameters":{"operation":"sweep","profile_points":[[0,0],[60,0],[60,12],[50,9],[40,7],[30,6],[20,7],[10,11],[0,20]],"path_points":[[0,0,0],[0,0,4]]},"material":null,"operations":[],"confidence":0.8}
2. "Vase 80mm tall and 40mm wide with a narrower neck" ->
{"part_type":"freeform","parameters":{"operation":"revolve","revolve_axis":"Y","revolve_angle_deg":360,"profile_points":[[0,0],[20,0],[19,20],[15,40],[10,60],[9,70],[10,80],[0,80]]},"material":null,"operations":[],"confidence":0.8}
3. "Loft from a 30mm square base to a 10mm square top, 25mm tall" ->
{"part_type":"freeform","parameters":{"operation":"loft","profiles":[{"points":[[-15,-15],[15,-15],[15,15],[-15,15]],"z":0},{"points":[[-5,-5],[5,-5],[5,5],[-5,5]],"z":25}]},"material":null,"operations":[],"confidence":0.8}

If the description doesn't clearly match any of these, set part_type to "unknown" and confidence to 0.0.
If a parameter isn't mentioned, omit it (the template applies its own default), don't guess a specific value for something the description didn't say.

Also extract, if mentioned:
- material: one of aluminum_6061, aluminum_6063, aluminum_7075, steel_1018, steel_4140, stainless_304, stainless_316, brass_c360, copper_c110, bronze_phosphor, titanium_ti6al4v, plastic_abs, plastic_pla, plastic_nylon, plastic_delrin, wood_plywood, wood_mdf
- operations: a list of {"type": "fillet"|"chamfer"|"shell", ...} for any fillet/chamfer/shell mentioned that isn't already a named parameter above

Respond with ONLY valid JSON, no markdown fences, no explanation, in this exact shape:
{
  "part_type": "...",
  "parameters": { ... },
  "material": "..." or null,
  "operations": [...],
  "confidence": 0.0-1.0
}"""


def _normalize_description(description: str) -> str:
    return re.sub(r"\s+", " ", description.strip()).casefold()


def _cache_key(description: str, model: str) -> str:
    payload = f"{_normalize_description(description)}\n{model}\n{PROMPT_VERSION}\n{VALIDATOR_VERSION}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parse_hash(data: dict) -> str:
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _make_result(
    data: dict,
    *,
    parser: str,
    model: str,
    retry_count: int = 0,
    warnings: list[str] | None = None,
) -> ParsedParameters:
    params = data.get("parameters", {})
    # `data` has already passed validate_llm_response before this function.
    result_for_hash = {
        "part_type": data.get("part_type", "unknown"),
        "parameters": params,
        "material": data.get("material"),
        "operations": data.get("operations", []),
        "confidence": data.get("confidence", 0.5),
    }
    return ParsedParameters(
        part_type=data.get("part_type", "unknown"),
        parameters=params,
        material=data.get("material"),
        operations=data.get("operations", []),
        assembly_parts=[],
        confidence=data.get("confidence", 0.5),
        warnings=warnings or [],
        parser=parser,
        model=model,
        prompt_version=PROMPT_VERSION,
        retry_count=retry_count,
        parse_hash=_parse_hash(result_for_hash),
    )


def _call_deepseek(
    client,
    model: str,
    description: str,
    failure_feedback: list[str] | None = None,
) -> dict:
    user_content = description
    if failure_feedback:
        user_content += (
            "\n\nThe previous parse was rejected by deterministic CAD validation. "
            "Fix these exact failures and return the complete JSON again:\n- "
            + "\n- ".join(failure_feedback)
        )

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        temperature=0.0,
        response_format={"type": "json_object"},
        extra_body={"thinking": {"type": "disabled"}},
    )

    raw_text = response.choices[0].message.content.strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.strip("`")
        if raw_text.startswith("json"):
            raw_text = raw_text[4:].strip()
    return json.loads(raw_text)


def _regex_result(
    description: str,
    *,
    parser: str,
    model: str | None,
    retry_count: int = 0,
    warning: str | None = None,
) -> ParsedParameters:
    """Regex parse with explicit provenance. `parser` is "regex" when the
    caller chose it, "regex_fallback" when DeepSeek was attempted and failed."""
    result = regex_fallback_parse(description)
    result.parser = parser
    result.model = model
    result.prompt_version = PROMPT_VERSION
    result.retry_count = retry_count
    result.parse_hash = _parse_hash({
        "part_type": result.part_type,
        "parameters": result.parameters,
        "material": result.material,
        "operations": result.operations,
        "confidence": result.confidence,
    })
    if warning:
        result.warnings.append(warning)
    return result


def _fallback_after_failure(description: str, model: str, reason: str) -> ParsedParameters:
    """DeepSeek was unreachable/unusable. Template parts can still be parsed by
    regex, but a freeform shape cannot: regex has no way to read a custom
    outline and would silently substitute an unrelated default shape."""
    result = _regex_result(
        description, parser="regex_fallback", model=model, retry_count=2,
        warning=f"AI parser unavailable ({reason}); regex fallback used",
    )
    if result.part_type == "freeform" or needs_ai_parser(description):
        raise ParseError(
            "This looks like a custom shape, which needs the AI parser, and it is "
            "temporarily unavailable. Please try again in a moment.",
            details={"parser": "regex_fallback", "model": model,
                     "prompt_version": PROMPT_VERSION, "retry_count": 2},
        )
    return result


def parse_with_deepseek(
    description: str,
    api_key: str,
    model: str = "deepseek-v4-flash",
) -> ParsedParameters:
    """
    Parse with DeepSeek, then apply deterministic schema/fidelity gates.

    - A validated parse is cached by normalized description + model + prompt
      version + validator version, so an identical prompt returns the
      identical part. Rejected output is never cached.
    - Output that fails the gate is retried once with the exact failures fed
      back. If a freeform parse still fails, ParseError is raised (422, with
      the reasons) rather than building a shape that contradicts the request.
    - Transport/JSON failures fall back to regex for template parts only.
    """
    key = _cache_key(description, model)

    try:
        cached = db.get_parse_cache(key)
    except Exception as exc:  # noqa: BLE001 - a cache problem must never block a parse
        logger.warning("parse cache read failed: %s", exc)
        cached = None
    if cached:
        logger.info("DeepSeek parse cache hit", extra={"parse_hash": cached.get("parse_hash")})
        return _make_result(
            cached,
            parser="cache",
            model=model,
            retry_count=int(cached.get("retry_count", 0)),
            warnings=list(cached.get("warnings", [])),
        )

    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=api_key,
            base_url=settings.DEEPSEEK_BASE_URL,
            timeout=settings.DEEPSEEK_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("DeepSeek setup error: %s", exc)
        return _fallback_after_failure(description, model, f"setup failed: {exc}")

    last_errors: list[str] = []
    fidelity_failure = False
    for attempt in range(2):
        try:
            data = _call_deepseek(
                client, model, description,
                failure_feedback=last_errors if attempt else None,
            )
        except Exception as exc:  # noqa: BLE001 - network, timeout, bad JSON
            last_errors = [f"DeepSeek response/API error: {exc}"]
            fidelity_failure = False
            logger.warning("DeepSeek attempt %s failed: %s", attempt + 1, exc)
            continue

        validated, errors = validate_llm_response(data, description)
        if validated is None:
            last_errors = errors
            fidelity_failure = isinstance(data, dict) and data.get("part_type") == "freeform"
            logger.warning("DeepSeek parse rejected (attempt %s): %s", attempt + 1, errors)
            continue

        result = _make_result(
            validated,
            parser="deepseek",
            model=model,
            retry_count=attempt,
            warnings=[f"Fidelity retry {attempt} applied"] if attempt else [],
        )
        try:
            db.put_parse_cache(
                key,
                description=description,
                model=model,
                prompt_version=PROMPT_VERSION,
                result=validated | {
                    "retry_count": attempt,
                    "warnings": result.warnings,
                    "parse_hash": result.parse_hash,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("parse cache write failed: %s", exc)
        return result

    if fidelity_failure:
        raise ParseError(
            "The AI could not produce geometry that matches your description ("
            + "; ".join(last_errors[:3])
            + "). Try stating the key dimensions and which edges are curved.",
            details={"parser": "deepseek", "model": model,
                     "prompt_version": PROMPT_VERSION, "retry_count": 2,
                     "fidelity_errors": last_errors},
        )
    return _fallback_after_failure(description, model, "; ".join(last_errors)[:200])


def parse_description(
    description: str,
    use_deepseek: bool | None = None,
    api_key: str | None = None,
    model: str = "deepseek-v4-flash",
) -> ParsedParameters:
    """Drop-in parser with explicit LLM/cache/regex provenance."""
    resolved_key = api_key or settings.DEEPSEEK_API_KEY
    if use_deepseek is not False and resolved_key:
        return parse_with_deepseek(description, resolved_key, model=model)
    # Regex was the deliberate choice (opted out, or no key configured).
    result = _regex_result(description, parser="regex", model=None)
    if needs_ai_parser(description):
        result.warnings.append(
            "Your description mentions curved or custom geometry, which the rule-based "
            "parser cannot model; the result may not match. Use the AI parser for this shape."
        )
    return result
