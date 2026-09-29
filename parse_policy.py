"""
Fail-closed parse policy: decide allow_ai / allow_regex / reject.

One function owns the decision from three inputs:
  - description
  - endpoint ("paid" = /generate, "preview" = /preview)
  - use_deepseek flag from the client

On paid /generate, if the shape needs AI, use_deepseek=False is ignored or
rejected (422 + refund). Preview may use regex, with a warning kept.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from freeform_validation import needs_ai_parser

Endpoint = Literal["paid", "preview"]
Decision = Literal["allow_ai", "allow_regex", "reject"]


@dataclass(frozen=True)
class ParsePolicyResult:
    decision: Decision
    reason: str
    """Human-readable reason; shown on 422 or kept as a warning on preview."""
    force_deepseek: bool
    """True when paid path must use AI regardless of the client flag."""


def decide_parse_policy(
    description: str,
    endpoint: Endpoint,
    use_deepseek: bool | None,
    *,
    deepseek_key_available: bool = True,
) -> ParsePolicyResult:
    """Decide how parsing is allowed for this request.

    Matrix (conceptual):
      endpoint × key present × flag × shape type (AI-needed or not)

    Rules:
      - If shape does not need AI: allow_regex is fine; AI is optional.
      - Preview: always may fall back to regex (warning if AI-needed).
      - Paid + AI-needed + (use_deepseek is False OR no key): reject.
      - Paid + AI-needed + key + use_deepseek not False: allow_ai (force).
      - Paid + not AI-needed: allow_regex (or AI if key + flag).
    """
    text = (description or "").strip()
    if not text:
        return ParsePolicyResult(
            decision="reject",
            reason="description must not be empty",
            force_deepseek=False,
        )

    needs_ai = needs_ai_parser(text)
    flag_false = use_deepseek is False
    flag_true = use_deepseek is True
    # None means auto: prefer AI when key exists.

    if endpoint == "preview":
        if needs_ai and (flag_false or not deepseek_key_available):
            return ParsePolicyResult(
                decision="allow_regex",
                reason=(
                    "Preview used regex fallback; this shape needs AI for a "
                    "faithful result. Re-submit via paid /generate with DeepSeek."
                ),
                force_deepseek=False,
            )
        if needs_ai and deepseek_key_available and not flag_false:
            return ParsePolicyResult(
                decision="allow_ai",
                reason="AI parser selected for freeform/organic shape",
                force_deepseek=True,
            )
        # Simple shapes on preview: regex is fine
        if flag_true and deepseek_key_available:
            return ParsePolicyResult(
                decision="allow_ai",
                reason="Client requested DeepSeek",
                force_deepseek=False,
            )
        return ParsePolicyResult(
            decision="allow_regex",
            reason="Regex parser sufficient for this description",
            force_deepseek=False,
        )

    # ---- paid (/generate) ----
    if needs_ai:
        if not deepseek_key_available:
            return ParsePolicyResult(
                decision="reject",
                reason=(
                    "This shape requires the AI parser, but no DeepSeek API key "
                    "is configured. You were not charged."
                ),
                force_deepseek=False,
            )
        if flag_false:
            return ParsePolicyResult(
                decision="reject",
                reason=(
                    "This shape requires the AI parser; use_deepseek=false is "
                    "not allowed on paid /generate. You were not charged."
                ),
                force_deepseek=False,
            )
        return ParsePolicyResult(
            decision="allow_ai",
            reason="Paid path forces AI for freeform/organic shape",
            force_deepseek=True,
        )

    # Not AI-needed on paid
    if flag_true and deepseek_key_available:
        return ParsePolicyResult(
            decision="allow_ai",
            reason="Client requested DeepSeek",
            force_deepseek=False,
        )
    if flag_false or not deepseek_key_available:
        return ParsePolicyResult(
            decision="allow_regex",
            reason="Regex parser sufficient for this description",
            force_deepseek=False,
        )
    # Auto + key: allow AI
    return ParsePolicyResult(
        decision="allow_ai",
        reason="Auto: DeepSeek available",
        force_deepseek=False,
    )
