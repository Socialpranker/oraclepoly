"""Tests для app.detectors.token_resolver."""

from __future__ import annotations

from app.detectors.token_resolver import (
    no_token_id,
    resolve_token_id,
    yes_token_id,
)

# Sample real-shape clob_token_ids (из markets table — Polymarket ERC-1155 token ids)
SAMPLE_TOKENS = [
    "15145237739766757543920295010173333079386008614623370487057624811543913015599",  # YES
    "22927363175825625095832888773253962860881299963299337794235022401608169522396",  # NO
]


def test_resolve_yes_returns_index_0():
    """Polymarket convention: index 0 = YES outcome."""
    assert resolve_token_id(SAMPLE_TOKENS, "YES") == SAMPLE_TOKENS[0]


def test_resolve_no_returns_index_1():
    """Polymarket convention: index 1 = NO outcome."""
    assert resolve_token_id(SAMPLE_TOKENS, "NO") == SAMPLE_TOKENS[1]


def test_yes_shortcut_matches_resolve():
    assert yes_token_id(SAMPLE_TOKENS) == resolve_token_id(SAMPLE_TOKENS, "YES")


def test_no_shortcut_matches_resolve():
    assert no_token_id(SAMPLE_TOKENS) == resolve_token_id(SAMPLE_TOKENS, "NO")


def test_case_insensitive_side():
    """Side normalized к UPPER (case-insensitive caller)."""
    assert resolve_token_id(SAMPLE_TOKENS, "yes") == SAMPLE_TOKENS[0]
    assert resolve_token_id(SAMPLE_TOKENS, " No ") == SAMPLE_TOKENS[1]
    assert resolve_token_id(SAMPLE_TOKENS, "Yes") == SAMPLE_TOKENS[0]


def test_returns_none_on_empty_list():
    assert resolve_token_id([], "NO") is None
    assert yes_token_id([]) is None
    assert no_token_id([]) is None


def test_returns_none_on_none_input():
    """Legacy markets без CLOB tokens (clob_token_ids IS NULL)."""
    assert resolve_token_id(None, "NO") is None
    assert yes_token_id(None) is None
    assert no_token_id(None) is None


def test_returns_none_on_non_list():
    """Невалидный JSONB (не list)."""
    assert resolve_token_id({}, "NO") is None
    assert resolve_token_id("not_a_list", "NO") is None
    assert resolve_token_id(42, "NO") is None


def test_returns_none_on_short_list():
    """Markets с одним token (broken state) → return None."""
    assert resolve_token_id(["only_yes"], "NO") is None
    assert resolve_token_id(["only_yes"], "YES") is None


def test_returns_none_on_invalid_side():
    assert resolve_token_id(SAMPLE_TOKENS, "MAYBE") is None
    assert resolve_token_id(SAMPLE_TOKENS, "") is None


def test_handles_null_token_in_list():
    """Если один из tokens IS NULL внутри valid array."""
    assert resolve_token_id([None, "no_token"], "YES") is None
    assert resolve_token_id(["yes_token", None], "NO") is None


def test_returns_string_even_for_int_tokens():
    """Tokens могут приходить как int → coerce to str."""
    result = resolve_token_id([12345, 67890], "YES")
    assert result == "12345"
    assert isinstance(result, str)


def test_jsonb_string_from_asyncpg_decoded():
    """asyncpg returns JSONB columns as raw JSON string by default."""
    raw = '["yes_token_abc", "no_token_xyz"]'
    assert resolve_token_id(raw, "YES") == "yes_token_abc"
    assert resolve_token_id(raw, "NO") == "no_token_xyz"
    assert no_token_id(raw) == "no_token_xyz"
    assert yes_token_id(raw) == "yes_token_abc"


def test_jsonb_string_with_real_polymarket_tokens():
    raw = (
        '["19373132388188911416033527979133630489259068118721620794308631858653063152694",'
        ' "16968516085779820972645096196098185885259391258673743128228986305756904636978"]'
    )
    assert no_token_id(raw) == "16968516085779820972645096196098185885259391258673743128228986305756904636978"


def test_invalid_jsonb_string_returns_none():
    """Malformed JSON → None (defensive)."""
    assert resolve_token_id("not valid json", "NO") is None
    assert resolve_token_id('{"yes": "a"}', "NO") is None  # dict instead of list
    assert resolve_token_id('["only_one"]', "NO") is None
