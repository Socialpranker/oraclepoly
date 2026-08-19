"""Tests для extract_gamma_extended_fields — pure function, no I/O.

Sample используется real Gamma response (Trump kiss market, 2026-05-21).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.jobs.market_sync import (
    _coerce_bool,
    _coerce_iso_datetime,
    _coerce_jsonb_list,
    _coerce_numeric,
    extract_gamma_extended_fields,
    process_market,
)

# === Coercion helpers ===


def test_coerce_jsonb_list_from_string() -> None:
    assert _coerce_jsonb_list('["proposed", "disputed"]') == '["proposed", "disputed"]'


def test_coerce_jsonb_list_from_python_list() -> None:
    assert _coerce_jsonb_list(["proposed", "disputed"]) == '["proposed", "disputed"]'


def test_coerce_jsonb_list_empty() -> None:
    assert _coerce_jsonb_list("[]") == "[]"
    assert _coerce_jsonb_list([]) == "[]"


def test_coerce_jsonb_list_none() -> None:
    assert _coerce_jsonb_list(None) is None
    assert _coerce_jsonb_list("") is None
    assert _coerce_jsonb_list("not json") is None
    assert _coerce_jsonb_list({"not": "list"}) is None


def test_coerce_numeric_handles_string_number() -> None:
    assert _coerce_numeric("500") == 500.0
    assert _coerce_numeric("0.04") == pytest.approx(0.04)


def test_coerce_numeric_handles_float() -> None:
    assert _coerce_numeric(0.4615) == pytest.approx(0.4615)


def test_coerce_numeric_none_for_invalid() -> None:
    assert _coerce_numeric(None) is None
    assert _coerce_numeric("") is None
    assert _coerce_numeric("not a number") is None


def test_coerce_bool() -> None:
    assert _coerce_bool(True) is True
    assert _coerce_bool(False) is False
    assert _coerce_bool(None) is None  # passthrough — не overwrite


def test_coerce_iso_datetime_passthrough_on_empty() -> None:
    assert _coerce_iso_datetime(None) is None
    assert _coerce_iso_datetime("") is None


def test_coerce_iso_datetime_parses_z_suffix() -> None:
    result = _coerce_iso_datetime("2026-05-15T19:50:49Z")
    assert result is not None
    assert result.year == 2026
    assert result.month == 5


# === extract_gamma_extended_fields ===


def _real_gamma_sample() -> dict:
    """Реальный sample Gamma /markets response, проверен 2026-05-21.

    Trump kiss market (id 2261347) — disputed UMA, full term-structure.
    """
    return {
        "id": "2261347",
        "conditionId": "0xcef981d46a1039b6ae02f578d2208302a8a3d63465d24363a1d65a86835a1ae8",
        "question": "Trump kiss by May 31?",
        "umaResolutionStatus": "disputed",
        "umaResolutionStatuses": '["proposed", "disputed", "proposed", "disputed"]',
        "umaBond": "500",
        "umaReward": "5",
        "oneHourPriceChange": 0.0,
        "oneDayPriceChange": 0.4615,
        "oneWeekPriceChange": 0.4615,
        "oneMonthPriceChange": 0.4615,
        "oneYearPriceChange": 0.4615,
        "cyom": False,
        "restricted": True,
        "negRiskAugmented": False,
        "automaticallyActive": True,
        "acceptingOrdersTimestamp": "2026-05-15T19:50:49Z",
        "deployingTimestamp": "2026-05-15T18:36:57.673778Z",
    }


def test_extract_real_sample_uma_fields() -> None:
    fields = extract_gamma_extended_fields(_real_gamma_sample())
    assert fields["uma_resolution_status"] == "disputed"
    assert fields["uma_bond"] == 500.0
    assert fields["uma_reward"] == 5.0
    # jsonb список сохраняется как JSON string
    parsed = json.loads(fields["uma_resolution_statuses"])
    assert parsed == ["proposed", "disputed", "proposed", "disputed"]


def test_extract_real_sample_term_structure() -> None:
    fields = extract_gamma_extended_fields(_real_gamma_sample())
    assert fields["one_hour_price_change"] == 0.0
    assert fields["one_day_price_change"] == pytest.approx(0.4615)
    assert fields["one_week_price_change"] == pytest.approx(0.4615)


def test_extract_real_sample_cohort_flags() -> None:
    fields = extract_gamma_extended_fields(_real_gamma_sample())
    assert fields["cyom"] is False
    assert fields["restricted"] is True
    assert fields["neg_risk_augmented"] is False
    assert fields["automatically_active"] is True


def test_extract_real_sample_lifecycle_timestamps() -> None:
    fields = extract_gamma_extended_fields(_real_gamma_sample())
    assert fields["accepting_orders_timestamp"].year == 2026
    assert fields["accepting_orders_timestamp"].month == 5
    assert fields["accepting_orders_timestamp"].day == 15


def test_extract_real_sample_raw_safety_net() -> None:
    fields = extract_gamma_extended_fields(_real_gamma_sample())
    raw_parsed = json.loads(fields["raw"])
    assert raw_parsed["question"] == "Trump kiss by May 31?"
    assert raw_parsed["umaBond"] == "500"


def test_extract_missing_fields_returns_none_safely() -> None:
    """Markets без UMA / term-structure (свежие markets) — должно return None без exceptions."""
    fields = extract_gamma_extended_fields({"id": "minimal-market"})
    assert fields["uma_resolution_status"] is None
    assert fields["uma_resolution_statuses"] is None
    assert fields["uma_bond"] is None
    assert fields["one_hour_price_change"] is None
    assert fields["cyom"] is None
    assert fields["accepting_orders_timestamp"] is None
    # raw всегда есть (полная сохранность)
    assert json.loads(fields["raw"]) == {"id": "minimal-market"}


def test_extract_handles_empty_strings_as_none() -> None:
    """Gamma иногда возвращает '' для отсутствующих numeric/timestamp."""
    fields = extract_gamma_extended_fields({"umaBond": "", "acceptingOrdersTimestamp": ""})
    assert fields["uma_bond"] is None
    assert fields["accepting_orders_timestamp"] is None


def test_extract_returns_all_keys() -> None:
    """Контракт: всегда полный набор ключей, даже на empty input."""
    fields = extract_gamma_extended_fields({})
    expected_keys = {
        "uma_resolution_status",
        "uma_resolution_statuses",
        "uma_bond",
        "uma_reward",
        "one_hour_price_change",
        "one_day_price_change",
        "one_week_price_change",
        "one_month_price_change",
        "one_year_price_change",
        "cyom",
        "restricted",
        "neg_risk_augmented",
        "automatically_active",
        "accepting_orders_timestamp",
        "deploying_timestamp",
        "competitive",
        "fee_schedule",
        "raw",
    }
    assert set(fields.keys()) == expected_keys


def test_extract_competitive_from_event_context() -> None:
    """competitive приходит из родительского event через _event_competitive
    (process_event кладёт перед циклом по markets)."""
    fields = extract_gamma_extended_fields({"_event_competitive": 0.8003})
    assert fields["competitive"] == 0.8003


def test_extract_competitive_none_when_no_event_context() -> None:
    """Прямой sync market без event-контекста — _event_competitive отсутствует,
    competitive = None. UPDATE c COALESCE($N, competitive) не затирает БД."""
    fields = extract_gamma_extended_fields({"id": "x", "conditionId": "y"})
    assert fields["competitive"] is None


def test_extract_competitive_zero_kept_as_zero() -> None:
    """0.0 — валидный competitive (рынок ниже liquidity-threshold), не None."""
    fields = extract_gamma_extended_fields({"_event_competitive": 0.0})
    assert fields["competitive"] == 0.0


# === fee_schedule ===


def test_extract_fee_schedule_real_gamma_sample() -> None:
    """Реальный Gamma sample (verified 2026-05-21):
    {exponent: 1, rate: 0.05, takerOnly: true, rebateRate: 0.25}."""
    fields = extract_gamma_extended_fields(
        {"feeSchedule": {"exponent": 1, "rate": 0.05, "takerOnly": True, "rebateRate": 0.25}}
    )
    parsed = json.loads(fields["fee_schedule"])
    assert parsed == {"rate": 0.05, "exponent": 1.0, "takerOnly": True, "rebateRate": 0.25}


def test_extract_fee_schedule_minimal_just_rate() -> None:
    """Только rate — exponent/takerOnly/rebateRate отсутствуют, schedule валиден."""
    fields = extract_gamma_extended_fields({"feeSchedule": {"rate": 0.04}})
    parsed = json.loads(fields["fee_schedule"])
    assert parsed == {"rate": 0.04}


def test_extract_fee_schedule_none_when_missing() -> None:
    """Без feeSchedule → None (UPDATE с COALESCE не затирает existing)."""
    fields = extract_gamma_extended_fields({"id": "x"})
    assert fields["fee_schedule"] is None


def test_extract_fee_schedule_none_when_no_rate() -> None:
    """feeSchedule без rate невалиден → None."""
    fields = extract_gamma_extended_fields(
        {"feeSchedule": {"exponent": 1, "takerOnly": True}}
    )
    assert fields["fee_schedule"] is None


def test_extract_fee_schedule_none_when_not_dict() -> None:
    """feeSchedule не dict — игнорируем."""
    fields = extract_gamma_extended_fields({"feeSchedule": "not a dict"})
    assert fields["fee_schedule"] is None


def test_extract_fee_schedule_zero_rate_kept() -> None:
    """rate=0 (fee-free market) — валидно, не None."""
    fields = extract_gamma_extended_fields({"feeSchedule": {"rate": 0}})
    parsed = json.loads(fields["fee_schedule"])
    assert parsed == {"rate": 0.0}


# === raw write-gate (process_market) ===
#
# Корень DB Size Alert по markets: market-sync тянет до 1500 closed events каждые
# 15 мин и БЕЗУСЛОВНО переписывал raw закрытым рынкам → cleanup нулит, sync
# возвращает обратно. raw — write-only (0 читателей в коде), для закрытого рынка
# бесполезен. Гейт по is_active у места записи убивает источник возврата.


def _extended_update_call(mock_fetchrow):
    """SQL+params второго fetchrow в process_market — extended-update (UPDATE ... raw = ...)."""
    for call in mock_fetchrow.call_args_list:
        sql = call[0][0]
        if "UPDATE markets SET" in sql and "raw" in sql:
            return call
    raise AssertionError("extended-update вызов с raw не найден")


@pytest.mark.anyio
async def test_process_market_skips_raw_for_closed_market() -> None:
    """closed=true рынок: raw в extended-update пишется как NULL (не сохраняем мёртвый груз)."""
    with patch("app.jobs.market_sync.fetchrow", new_callable=AsyncMock, return_value={"id": "uuid-1"}) as mock_fr:
        await process_market({"id": "m1", "conditionId": "c1", "closed": True})

    sql = _extended_update_call(mock_fr)[0][0]
    assert "CASE WHEN $20" in sql
    assert "ELSE NULL END" in sql
    # is_active прокидывается параметром-гейтом, не хардкод в SQL
    params = _extended_update_call(mock_fr)[0]
    assert False in params  # is_active=False для closed-рынка


@pytest.mark.anyio
async def test_process_market_writes_raw_for_active_market() -> None:
    """Активный рынок (closed отсутствует): raw сохраняется как обычно."""
    with patch("app.jobs.market_sync.fetchrow", new_callable=AsyncMock, return_value={"id": "uuid-2"}) as mock_fr:
        await process_market({"id": "m2", "conditionId": "c2", "question": "Active?"})

    call = _extended_update_call(mock_fr)
    sql = call[0][0]
    assert "CASE WHEN $20" in sql
    assert True in call[0]  # is_active=True → ветка CASE пишет raw
