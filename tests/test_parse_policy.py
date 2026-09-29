"""Fail-closed parse policy matrix: endpoint × key × flag × shape type."""
from __future__ import annotations

import pytest

from parse_policy import decide_parse_policy

CUBE = "a simple 50mm cube"
SHARK = "organic freeform shark-fin-shaped curve sweep profile"


@pytest.mark.parametrize(
    "description,endpoint,use_deepseek,key,expected_decision",
    [
        # Simple shape, paid
        (CUBE, "paid", False, True, "allow_regex"),
        (CUBE, "paid", True, True, "allow_ai"),
        (CUBE, "paid", None, True, "allow_ai"),
        (CUBE, "paid", None, False, "allow_regex"),
        # AI-needed, paid: reject when flag false or no key
        (SHARK, "paid", False, True, "reject"),
        (SHARK, "paid", None, False, "reject"),
        (SHARK, "paid", False, False, "reject"),
        (SHARK, "paid", True, True, "allow_ai"),
        (SHARK, "paid", None, True, "allow_ai"),
        # Preview: never reject; may warn via regex
        (SHARK, "preview", False, True, "allow_regex"),
        (SHARK, "preview", None, False, "allow_regex"),
        (SHARK, "preview", True, True, "allow_ai"),
        (CUBE, "preview", False, True, "allow_regex"),
        (CUBE, "preview", True, True, "allow_ai"),
    ],
)
def test_policy_matrix(description, endpoint, use_deepseek, key, expected_decision):
    r = decide_parse_policy(
        description, endpoint, use_deepseek, deepseek_key_available=key
    )
    assert r.decision == expected_decision


def test_paid_ai_needed_forces_deepseek():
    r = decide_parse_policy(SHARK, "paid", None, deepseek_key_available=True)
    assert r.decision == "allow_ai"
    assert r.force_deepseek is True


def test_reject_reason_mentions_not_charged():
    r = decide_parse_policy(SHARK, "paid", False, deepseek_key_available=True)
    assert r.decision == "reject"
    assert "not charged" in r.reason.lower() or "were not charged" in r.reason.lower()


def test_empty_description_rejected():
    r = decide_parse_policy("  ", "paid", None, deepseek_key_available=True)
    assert r.decision == "reject"
