"""Тесты parse_jsonb — нормализация asyncpg JSONB значений."""

from __future__ import annotations

from app.services.jsonb_utils import parse_jsonb


def test_parse_none() -> None:
    assert parse_jsonb(None) is None


def test_parse_empty_string() -> None:
    assert parse_jsonb("") is None


def test_parse_invalid_json_string() -> None:
    assert parse_jsonb("not a json") is None


def test_parse_valid_json_object() -> None:
    assert parse_jsonb('{"rate": 0.05, "exponent": 1}') == {"rate": 0.05, "exponent": 1}


def test_parse_dict_passthrough() -> None:
    """Если asyncpg уже декодирует JSONB → dict (set_type_codec configured),
    passthrough без перепарсинга."""
    assert parse_jsonb({"rate": 0.05}) == {"rate": 0.05}


def test_parse_array_returns_none() -> None:
    """Мы храним только JSON object-shape, массивы возвращают None."""
    assert parse_jsonb("[1, 2, 3]") is None


def test_parse_scalar_returns_none() -> None:
    """Скаляры внутри JSON тоже отбрасываем."""
    assert parse_jsonb('"just a string"') is None
    assert parse_jsonb("42") is None
    assert parse_jsonb("null") is None


def test_parse_real_fee_schedule_sample() -> None:
    """Реальный Gamma feeSchedule JSON — точный roundtrip."""
    raw = '{"rate": 0.05, "exponent": 1, "takerOnly": true, "rebateRate": 0.25}'
    result = parse_jsonb(raw)
    assert result == {"rate": 0.05, "exponent": 1, "takerOnly": True, "rebateRate": 0.25}


def test_parse_unknown_type_returns_none() -> None:
    """Числа/списки/прочее на входе (не str/dict/None) → None."""
    assert parse_jsonb(42) is None
    assert parse_jsonb(["x", "y"]) is None
