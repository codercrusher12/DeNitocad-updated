"""DeepSeek parse flow: retry, cache, provenance, and the no-silent-wrong-shape rule.

The OpenAI client and the parse cache are replaced, so no network or DB is used.
"""
from __future__ import annotations

import sys
import types

import pytest

import deepseek_parser as dp
from exceptions import ParseError

SHARK = ("A freeform shark-fin-shaped flat plate, 50mm tall and 30mm long at the base. "
         "The leading edge is a curved sweep, and the trailing edge curves inward (concave). "
         "Constant thickness of 5mm, sharp corners.")
PATH5 = [[0, 0, 0], [0, 0, 5]]


def fin(points):
    return {"part_type": "freeform", "confidence": 0.9, "material": None, "operations": [],
            "parameters": {"operation": "sweep", "profile_points": points, "path_points": PATH5}}


GOOD = fin([[0, 0], [30, 0], [27, 8], [22, 14], [19, 22], [20, 32], [18, 42], [12, 47], [0, 50]])
TRIANGLE = fin([[0, 0], [30, 0], [30, 50], [0, 0]])


@pytest.fixture
def env(monkeypatch):
    cache: dict = {}
    calls: list = []
    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))
    monkeypatch.setattr(dp.db, "get_parse_cache", lambda k: cache.get(k))
    monkeypatch.setattr(dp.db, "put_parse_cache", lambda k, **kw: cache.__setitem__(k, kw["result"]))

    def script(*responses):
        it = iter(responses)

        def fake(client, model, description, failure_feedback=None):
            calls.append(failure_feedback)
            r = next(it)
            if isinstance(r, Exception):
                raise r
            return r
        monkeypatch.setattr(dp, "_call_deepseek", fake)

    return types.SimpleNamespace(cache=cache, calls=calls, script=script)


def test_good_first_answer_is_cached_and_replayed_identically(env):
    env.script(GOOD)
    first = dp.parse_with_deepseek(SHARK, "key")
    assert (first.parser, first.retry_count) == ("deepseek", 0)
    assert len(env.cache) == 1

    env.script()  # any further API call would raise StopIteration
    second = dp.parse_with_deepseek(SHARK, "key")
    assert second.parser == "cache"
    assert second.parameters == first.parameters
    assert second.parse_hash == first.parse_hash


def test_triangle_then_good_retries_with_exact_reasons(env):
    env.script(TRIANGLE, GOOD)
    result = dp.parse_with_deepseek(SHARK, "key")
    assert (result.parser, result.retry_count) == ("deepseek", 1)
    assert env.calls[0] is None and any("at least 8" in e for e in env.calls[1])
    assert len(result.parameters["profile_points"]) == 9


def test_two_bad_freeform_answers_raise_instead_of_building_a_wrong_part(env):
    env.script(TRIANGLE, TRIANGLE)
    with pytest.raises(ParseError) as exc:
        dp.parse_with_deepseek(SHARK, "key")
    assert exc.value.details["parser"] == "deepseek"
    assert exc.value.details["retry_count"] == 2
    assert env.cache == {}          # rejected output is never cached


def test_api_failure_falls_back_to_regex_for_template_parts(env):
    env.script(RuntimeError("timeout"), RuntimeError("timeout"))
    result = dp.parse_with_deepseek("shaft 10mm diameter, 50mm long", "key")
    assert result.parser == "regex_fallback" and result.part_type == "shaft"
    assert any("unavailable" in w for w in result.warnings)


def test_api_failure_never_falls_back_to_a_default_freeform_shape(env):
    env.script(RuntimeError("timeout"), RuntimeError("timeout"))
    with pytest.raises(ParseError):
        dp.parse_with_deepseek(SHARK, "key")


def test_cache_write_failure_does_not_break_the_parse(env, monkeypatch):
    env.script(GOOD)
    monkeypatch.setattr(dp.db, "put_parse_cache", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    assert dp.parse_with_deepseek(SHARK, "key").parser == "deepseek"


def test_cache_key_changes_with_prompt_and_validator_versions(monkeypatch):
    base = dp._cache_key(SHARK, "m")
    monkeypatch.setattr(dp, "PROMPT_VERSION", "other")
    assert dp._cache_key(SHARK, "m") != base
    monkeypatch.undo()
    monkeypatch.setattr(dp, "VALIDATOR_VERSION", "other")
    assert dp._cache_key(SHARK, "m") != base


def test_cache_key_ignores_whitespace_and_case():
    assert dp._cache_key("  Shaft   10MM ", "m") == dp._cache_key("shaft 10mm", "m")


def test_deliberate_regex_is_labelled_regex_not_fallback():
    r = dp.parse_description("shaft 10mm diameter, 50mm long", use_deepseek=False)
    assert r.parser == "regex"


def test_deliberate_regex_warns_when_the_shape_needs_the_ai_parser():
    r = dp.parse_description(SHARK, use_deepseek=False)
    assert r.parser == "regex"
    assert any("cannot model" in w for w in r.warnings)
