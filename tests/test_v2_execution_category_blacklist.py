"""V2 Execution — per-detector signal-level gates tests.

Покрывает два независимых reject-фильтра:
  1. _is_category_blacklisted — Sports for losing detectors.
  2. _is_entry_price_out_of_range — D3 mid-zone only ([0.10, 0.60]).


Эмпирика 2026-05-18 (14d virtual, см. memory):
  Sports = -$5,818 net / -9.3% ROI. 9 из 11 детекторов в Sports негативны.
  D36_obi_pressure около-нулевой (+0.9% ROI) — НЕ блочим.
  D13_retail_confidence_fade micro-positive (+12%, n=3) — НЕ блочим.

D3_cross_mkt_arb исключён из Sports-блэклиста 2026-07-05 (research/fable-gates-audit-2026-07-03/,
honest counterfactual по detector_signals за 60d/170 signals устойчиво позитивен и усиливается:
последние 3 недели 0 LOSS, avg ROI net 14-44%/неделя). LGB остаётся заблокирован — там
блэклист экономит ~$29k/мес paper.

Контракт _is_category_blacklisted(detector, category):
  - blacklisted detector × blacklisted category → True
  - blacklisted detector × неблокируемая category → False
  - unblacklisted detector × любая category → False
  - None category → False (защита от missing data)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.jobs import v2_execution
from app.jobs.v2_execution import (
    _CATEGORY_BLACKLIST,
    _FILL_PRICE_RANGE,
    _gate_shadow_enabled,
    _is_category_blacklisted,
    _is_entry_price_out_of_range,
)


def test_d36_obi_pressure_sports_allowed():
    """D36 единственный неубыточный в Sports — НЕ блочим."""
    assert _is_category_blacklisted("D36_obi_pressure", "Sports") is False


def test_d13_sports_allowed():
    """D13 micro-positive в Sports, НЕ блочим."""
    assert _is_category_blacklisted("D13_retail_confidence_fade", "Sports") is False


def test_unknown_detector_never_blacklisted():
    """Незнакомый детектор — fallback к unblocked, чтобы не остановить новые feature flags."""
    assert _is_category_blacklisted("D99_unknown", "Sports") is False


def test_none_category_safe():
    """sig['category'] может быть None в transient state — не падаем."""
    assert _is_category_blacklisted("D31_underdog_backing", None) is False


def test_empty_string_category_safe():
    """Empty string не считается заблокированной категорией."""
    assert _is_category_blacklisted("D31_underdog_backing", "") is False


@pytest.mark.parametrize(
    "detector",
    [
        "STANDALONE_V2_LGB",
    ],
)
def test_all_losers_blocked_in_sports(detector: str):
    """Все убыточные в Sports детекторы должны блокироваться."""
    assert _is_category_blacklisted(detector, "Sports") is True


def test_d3_sports_allowed():
    """D3 исключён 2026-07-05 — структурный арбитраж, другая механика риска."""
    assert _is_category_blacklisted("D3_cross_mkt_arb", "Sports") is False


@pytest.mark.parametrize("category", ["Crypto", "World & Economy"])
def test_d38_blocked_in_losing_categories(category: str):
    """D38 заблокирован 2026-07-07: honest 14d virtual — W&E −$2,127, Crypto −$1,217."""
    assert _is_category_blacklisted("D38_resolution_lag", category) is True


@pytest.mark.parametrize("category", ["Sports", "Politics", "Tech & AI", "Culture"])
def test_d38_other_categories_allowed(category: str):
    """Блэклист D38 точечный — остальные категории не режем (Politics вообще в плюсе)."""
    assert _is_category_blacklisted("D38_resolution_lag", category) is False


def test_blacklist_is_frozenset_immutable():
    """Защита: blacklist значения — frozenset, чтобы случайно не модифицировать в runtime."""
    for det, cats in _CATEGORY_BLACKLIST.items():
        assert isinstance(cats, frozenset), f"{det}: expected frozenset, got {type(cats).__name__}"


# --- _is_entry_price_out_of_range (D3 mid-zone gate) ---------------------
# Эмпирика 2026-05-18: D3 mid [0.10, 0.60] = +$3,346, extremes = −$3,989.


def test_d3_below_range_rejected():
    """D3 entry_price=0.05 (extreme low) — отбрасываем."""
    assert _is_entry_price_out_of_range("D3_cross_mkt_arb", 0.05) is True


def test_d3_above_range_rejected():
    """D3 entry_price=0.70 (high zone) — отбрасываем."""
    assert _is_entry_price_out_of_range("D3_cross_mkt_arb", 0.70) is True


def test_d3_at_lower_boundary_accepted():
    """D3 entry_price=0.10 (нижняя граница включительно) — пропускаем."""
    assert _is_entry_price_out_of_range("D3_cross_mkt_arb", 0.10) is False


def test_d3_at_upper_boundary_accepted():
    """D3 entry_price=0.60 (верхняя граница включительно) — пропускаем."""
    assert _is_entry_price_out_of_range("D3_cross_mkt_arb", 0.60) is False


def test_d3_mid_zone_accepted():
    """D3 entry_price=0.42 (mid sweet spot) — пропускаем."""
    assert _is_entry_price_out_of_range("D3_cross_mkt_arb", 0.42) is False


def test_unknown_detector_no_range_constraint():
    """Незнакомый детектор — no constraint, fallback к unblocked."""
    assert _is_entry_price_out_of_range("D99_unknown", 0.05) is False
    assert _is_entry_price_out_of_range("D99_unknown", 0.95) is False


def test_none_entry_price_safe():
    """entry_price=None (transient data) — не падаем, не блокируем."""
    assert _is_entry_price_out_of_range("D3_cross_mkt_arb", None) is False


def test_fill_price_range_uses_tuples():
    """Защита формата: значения — (lo, hi) tuples."""
    for det, rng in _FILL_PRICE_RANGE.items():
        assert isinstance(rng, tuple), f"{det}: expected tuple, got {type(rng).__name__}"
        assert len(rng) == 2, f"{det}: expected (lo, hi), got {rng}"
        lo, hi = rng
        assert lo < hi, f"{det}: lo={lo} must be < hi={hi}"


# --- _gate_shadow_enabled (shadow-телеметрия для гейтов на пересмотре) ---
# D3 снят из _GATE_SHADOW_TARGETS 2026-07-05 вместе с исключением из блэклиста —
# гейта, который он отслеживал (category_blacklist:Sports), для D3 больше нет.
# D38 добавлен 2026-07-07 вместе со своим блэклистом Crypto/W&E (пересмотр ~07-21).


def test_d3_category_blacklist_shadow_not_enabled():
    """D3 больше не отслеживается — гейт снят, шейдоу-таргет для него удалён."""
    assert _gate_shadow_enabled("D3_cross_mkt_arb", "category_blacklist") is False


def test_lgb_category_blacklist_shadow_not_enabled():
    """LGB намеренно исключён — 9,390 rejects/30д против 145 у D3 (evidence/04)."""
    assert _gate_shadow_enabled("STANDALONE_V2_LGB", "category_blacklist") is False


def test_d38_category_blacklist_shadow_enabled():
    """D38-блэклист новый (2026-07-07) — книга на момент reject копится для пересмотра."""
    assert _gate_shadow_enabled("D38_resolution_lag", "category_blacklist") is True


def test_d3_other_gate_shadow_not_enabled():
    assert _gate_shadow_enabled("D3_cross_mkt_arb", "thin_book") is False


def test_unknown_detector_shadow_not_enabled():
    assert _gate_shadow_enabled("D99_unknown", "category_blacklist") is False


@pytest.mark.asyncio
async def test_process_signal_no_shadow_on_lgb_sports_reject():
    """LGB + Sports reject → тот же reject-путь, но БЕЗ shadow-телеметрии (не в _GATE_SHADOW_TARGETS)."""
    sig = {
        "detector": "STANDALONE_V2_LGB",
        "detector_signal_id": "00000000-0000-0000-0000-0000000lgb1",
        "market_id": "0xMARKET",
        "event_id": "0xEVENT",
        "side": "NO",
        "category": "Sports",
        "entry_price": 0.42,
        "calibrated_p": 0.55,
        "meta_p": 0.55,
        "token_id": "0xTOKEN",
    }

    with (
        patch.object(v2_execution, "_insert_order", new=AsyncMock(return_value="order-1")),
        patch(
            "app.services.gate_shadow_telemetry.log_gate_shadow", new=AsyncMock()
        ) as mock_shadow,
    ):
        result = await v2_execution._process_signal(sig)

    assert result == "rejected"
    mock_shadow.assert_not_awaited()
