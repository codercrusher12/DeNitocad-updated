"""
Conversational prompt refinement - a perk for wallets currently sitting
on a bulk-pack-sized credit balance (see config.py's
BULK_TIER_CREDIT_THRESHOLD and web_app.py's POST /prompt-refine).

This is deliberately NOT the same code path as deepseek_parser.py.
That module extracts structured parameters from a finished description
and hands them to the CAD engine - it's the last step before geometry
gets built. This module is the opposite end: a back-and-forth chat that
helps someone WRITE a better description in the first place (asking
what's missing, suggesting dimensions, tightening ambiguous phrasing),
with no access to the CAD engine at all and no attempt to output
structured data. The user copies the refined wording over to the real
generate/preview flow themselves when they're happy with it - this
module never calls generator.generate_from_text or anything downstream
of it, on purpose, so a bug here can't accidentally trigger a billed
generation.

UNVERIFIED IN THIS ENVIRONMENT: same caveat as deepseek_parser.py - no
internet access or DeepSeek API key available here, written against
DeepSeek's documented OpenAI-compatible API but not run against it.
"""

from __future__ import annotations

from config import settings
from logging_config import get_logger

logger = get_logger(__name__)


class PromptRefineError(Exception):
    """Raised on any failure talking to DeepSeek. There's no regex
    fallback here the way deepseek_parser.py has one - a refinement
    chat with no LLM behind it isn't a degraded version of the
    feature, it's not the feature at all, so this surfaces as a clear
    error instead of silently returning something unhelpful."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


SYSTEM_PROMPT = """You are a CAD description assistant for NitoCAD. Your ONLY job \
is to help the user write a clearer, more complete natural-language description of a \
mechanical part BEFORE they submit it for generation. You do not generate CAD models, \
you do not extract parameters, you do not output JSON, and you have no ability to \
create any file - you are a conversational writing aid, nothing else.

When the user describes a part:
- Point out dimensions or details that are missing and would matter for manufacturing \
(e.g. wall thickness, hole diameters, fillet radii, material, tolerances).
- Ask short, specific clarifying questions rather than long lists - one or two at a time.
- Suggest concrete phrasing improvements when something is ambiguous (e.g. "4 holes" -> \
"4 holes, 5mm diameter, evenly spaced on a 40mm bolt circle").
- If the description is already clear and complete, say so plainly and suggest they \
copy it into the generator - don't manufacture busywork by inventing more questions.

NitoCAD's generator understands these part types and their key parameters, for \
reference (use this to know what's worth asking about, not as a menu to recite): \
motor_mount, l_bracket, flat_plate, simple_box, shaft, bearing, spacer, washer, \
sphere, gear, pulley, sprocket, structural_beam, angle, tube, pipe_fitting, flange, \
hinge, cam, hex_standoff, t_bracket, channel_bracket, connecting_rod, crankshaft, \
and freeform (for irregular/custom-profile shapes).

Never claim to have generated, previewed, or exported anything - you can only discuss \
wording. If the user asks you to actually generate the part, tell them to copy the \
finished description into the generator themselves."""


def refine_prompt(messages: list[dict], api_key: str, model: str) -> str:
    """messages: the conversation so far, as [{"role": "user"|"assistant",
    "content": str}, ...] - already trimmed/validated by the caller (see
    web_app.py's POST /prompt-refine for the length caps; this function
    trusts its input). Returns the assistant's next reply as plain text.

    Raises PromptRefineError on any failure - see that class's docstring
    for why there's no fallback path here."""
    try:
        from openai import OpenAI  # DeepSeek's API is OpenAI-compatible

        client = OpenAI(
            api_key=api_key,
            base_url=settings.DEEPSEEK_BASE_URL,
            timeout=settings.DEEPSEEK_TIMEOUT_SECONDS,
        )

        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": SYSTEM_PROMPT}, *messages],
            temperature=0.4,  # some room for natural phrasing suggestions,
            # well short of creative-writing temperatures - this is still
            # meant to give consistent, actionable advice, not vary wildly
            # call to call.
            # Same reasoning as deepseek_parser.py: this is a fast,
            # structured-ish task (even though the output is prose, not
            # JSON), not one that benefits from spending reasoning tokens.
            extra_body={"thinking": {"type": "disabled"}},
        )
        return response.choices[0].message.content.strip()

    except Exception as exc:  # noqa: BLE001 - genuinely any API/network error
        logger.warning("prompt refinement DeepSeek call failed: %s", exc)
        raise PromptRefineError(
            "Couldn't reach the refinement assistant right now - try again shortly."
        ) from exc
