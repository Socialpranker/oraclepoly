import pytest


def test_parse_plain_json():
    from app.core.parsing import parse_json_from_llm_response

    assert parse_json_from_llm_response('{"key": "value"}') == {"key": "value"}


def test_parse_json_with_markdown_fences():
    from app.core.parsing import parse_json_from_llm_response

    assert parse_json_from_llm_response('```json\n{"key": "value"}\n```') == {"key": "value"}


def test_parse_json_with_plain_fences():
    from app.core.parsing import parse_json_from_llm_response

    assert parse_json_from_llm_response('```\n{"key": "value"}\n```') == {"key": "value"}


def test_parse_invalid_json_raises():
    from app.core.parsing import parse_json_from_llm_response

    with pytest.raises(ValueError, match="LLM JSON parse error"):
        parse_json_from_llm_response("not json at all")
