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

import json

from config import settings
from logging_config import get_logger
from smart_parser import ParsedParameters, parse_description as regex_fallback_parse

logger = get_logger(__name__)


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
- freeform: for anything irregular/organic/custom-profile that doesn't match a template above. operation ("revolve"|"sweep"|"loft"), profile_points (list of [x,y] mm pairs tracing the 2D cross-section/profile in order - this is the actual shape, extract it as precisely as the description allows), revolve_axis ("X"|"Y", for revolve), revolve_angle_deg (default 360, for revolve), path_points (list of [x,y,z] mm points, for sweep)

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


def parse_with_deepseek(description: str, api_key: str, model: str = "deepseek-v4-flash") -> ParsedParameters:
    """
    Real LLM call to DeepSeek's chat completions API (OpenAI-compatible
    format). Falls back to the regex parser on any failure, same
    resilience pattern the original llm_parser.py used for Claude.

    model: "deepseek-v4-flash" (faster, cheaper) or "deepseek-v4-pro"
    (DeepSeek's current model names as of this build, verify against
    https://api-docs.deepseek.com if this stops working, DeepSeek has
    renamed models before, deepseek-chat/deepseek-reasoner were retired
    after July 24, 2026).
    """
    try:
        from openai import OpenAI  # DeepSeek's API is OpenAI-compatible

        # Explicit timeout - without one, a stalled DeepSeek call blocks
        # the request (and, since generate_from_text runs synchronously,
        # a whole worker) indefinitely instead of falling back to the
        # regex parser like every other failure mode here does.
        client = OpenAI(
            api_key=api_key,
            base_url=settings.DEEPSEEK_BASE_URL,
            timeout=settings.DEEPSEEK_TIMEOUT_SECONDS,
        )

        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": description},
            ],
            temperature=0.1,  # low temperature, this is extraction, not creative writing
            # deepseek-v4-flash defaults to thinking mode ON as of the
            # July 2026 model-name migration - this is structured
            # extraction, not a task that benefits from reasoning tokens,
            # so disable it explicitly or every call silently gets slower
            # and more expensive than intended.
            # https://api-docs.deepseek.com/guides/reasoning_model
            extra_body={"thinking": {"type": "disabled"}},
        )

        raw_text = response.choices[0].message.content.strip()

        # Strip markdown fences if the model added them despite instructions,
        # models don't always perfectly follow "no markdown" instructions
        if raw_text.startswith("```"):
            raw_text = raw_text.strip("`")
            if raw_text.startswith("json"):
                raw_text = raw_text[4:].strip()

        data = json.loads(raw_text)

        return ParsedParameters(
            part_type=data.get("part_type", "unknown"),
            parameters=data.get("parameters", {}),
            material=data.get("material"),
            operations=data.get("operations", []),
            assembly_parts=[],
            confidence=data.get("confidence", 0.5),
            warnings=[],
        )

    except Exception as e:
        logger.warning("DeepSeek API error, falling back to regex parser: %s", e)
        fallback = regex_fallback_parse(description)
        fallback.warnings.append(f"DeepSeek call failed ({e}), used regex fallback instead")
        return fallback


def parse_description(description: str, use_deepseek: bool | None = None,
                       api_key: str | None = None, model: str = "deepseek-v4-flash") -> ParsedParameters:
    """
    Drop-in replacement for smart_parser.parse_description with an added
    real-LLM path.

    use_deepseek=None (default, "auto"): use DeepSeek if a key resolves
    from anywhere (a caller-supplied api_key, or DEEPSEEK_API_KEY in this
    server's own environment), otherwise silently use the regex fallback.
    This is what lets the public demo not show a DeepSeek toggle at all -
    the server decides based on whether it has been given a key, not the
    caller. API/agent consumers that DO want explicit control still have
    it: pass use_deepseek=True/False to override the auto behavior.

    use_deepseek=True forces DeepSeek (falls back to regex if no key
    resolves anywhere). use_deepseek=False forces regex even if a key is
    available - useful for a caller who wants deterministic, LLM-free
    parsing on purpose.
    """
    resolved_key = api_key or settings.DEEPSEEK_API_KEY

    if use_deepseek is False:
        return regex_fallback_parse(description)
    if (use_deepseek is True or use_deepseek is None) and resolved_key:
        return parse_with_deepseek(description, resolved_key, model=model)
    return regex_fallback_parse(description)
