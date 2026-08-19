"""Unit tests для evaluate_promote_readiness — единый расчёт promote-гейтов
virtual→live (Phase 2 audit unification: TG promote-callback и
/v2/lifecycle/live_readiness теперь зовут одну функцию).

Покрывает:
  - N/wins/losses читаются из period (кумулятивно от promoted_at) — НЕ recency.
  - sum_roi_net/Sortino/median_entry читаются из recent (30д-окно) — НЕ period.
  - Регрессия: низкочастотный детектор (мало сигналов/7d) — period накопил
    достаточно N кумулятивно, recent даёт честную свежую экономику; раньше
    (fetch_shadow_signals_7d, фиксированное 7д-окно) N был бы недостижим.
  - fetch_recent_window_signals: пустой список detectors, days!=30 → ValueError.
  - fetch_live_metrics_7d SQL: правильные статусы/execution_mode (текстовая
    проверка — без похода в БД, по образцу test_detector_signal_service_order.py).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services.detector_metrics import (
    _LIVE_METRICS_7D_SQL,
    evaluate_promote_readiness,
    fetch_recent_window_signals,
)

# ── evaluate_promote_readiness: period vs recent split ────────────────────────


@pytest.mark.asyncio
async def test_n_terminal_from_period_not_recent():
    """n_terminal/wins читаются из period (кумулятив), даже если recent пуст.

    Низкочастотный детектор (D3-class, ~0.37 сигн/день): за 30д-recency
    почти нет terminal сигналов, но кумулятивно от promoted_at (несколько
    месяцев) N≥30 накопился. Раньше (fetch_shadow_signals_7d, 7д-окно)
    N был бы недостижим НИКОГДА для такого детектора — это и был баг."""
    period = {"terminal_7d": 40, "wins_7d": 30}  # кумулятивно за долгий period
    recent = {  # 30д-окно почти пусто (низкая частота сигналов)
        "sum_roi_net_7d": 5.0,
        "mean_roi_net_7d": 2.0,
        "sum_neg_sq_roi_7d": 0.0,
        "median_entry_price_7d": None,
    }
    verdict = await evaluate_promote_readiness(
        "D3_cross_mkt_arb",
        promoted_at="2026-01-01",
        days_virtual=60,
        adversarial_pass=True,
        execution_enabled=True,
        period=period,
        recent=recent,
        virtual_metrics={"n_placed": 40, "n_filled": 35, "n_would_have_filled": 0},
    )
    assert verdict["gates"]["n_min"]["passed"] is True
    assert verdict["gates"]["n_min"]["value"] == 40


@pytest.mark.asyncio
async def test_economics_from_recent_not_period():
    """sum_roi_net/Sortino читаются из recent (30д), НЕ из period (all-time).

    Регрессия против "all-time маскирует затухающий edge": period накопил
    большую положительную сумму ROI за долгую историю, но recent (последние
    30д) показывает детектор уже слитым — gate должен FAIL по recent, не
    проигнорировать деградацию из-за жирного исторического period."""
    period = {"terminal_7d": 50, "wins_7d": 40}  # много N, WR историческ. высокий
    recent = {  # но recency экономика отрицательна — детектор деградировал
        "sum_roi_net_7d": -20.0,
        "mean_roi_net_7d": -2.0,
        "sum_neg_sq_roi_7d": 100.0,
        "median_entry_price_7d": 0.5,
    }
    verdict = await evaluate_promote_readiness(
        "D_decaying",
        promoted_at="2026-01-01",
        days_virtual=90,
        adversarial_pass=True,
        execution_enabled=True,
        period=period,
        recent=recent,
        virtual_metrics={"n_placed": 50, "n_filled": 45, "n_would_have_filled": 0},
    )
    # n_min passes (period N=50 ≥ 30), но pnl_positive fails (recent economics <0)
    assert verdict["gates"]["n_min"]["passed"] is True
    assert verdict["gates"]["pnl_positive"]["passed"] is False
    assert verdict["gates"]["pnl_positive"]["sum_roi_net_pct"] == -20.0
    assert verdict["decision"] != "READY"


@pytest.mark.asyncio
async def test_all_gates_pass_returns_ready():
    """Здоровый боец: N достаточно (period), экономика позитивна (recent),
    adversarial pass, days≥14, execution_enabled → READY."""
    period = {"terminal_7d": 50, "wins_7d": 40}
    recent = {
        "sum_roi_net_7d": 120.0,
        "mean_roi_net_7d": 2.4,
        "sum_neg_sq_roi_7d": 0.0,  # нет downside → Sortino=inf
        "median_entry_price_7d": None,
    }
    verdict = await evaluate_promote_readiness(
        "D_healthy",
        promoted_at="2026-01-01",
        days_virtual=20,
        adversarial_pass=True,
        execution_enabled=True,
        period=period,
        recent=recent,
        virtual_metrics={"n_placed": 50, "n_filled": 48, "n_would_have_filled": 0},
    )
    assert verdict["decision"] == "READY"


@pytest.mark.asyncio
async def test_adversarial_none_blocks_manual_override():
    """adversarial_pass=None → MANUAL_OVERRIDE_NEEDED, независимо от экономики."""
    period = {"terminal_7d": 50, "wins_7d": 40}
    recent = {
        "sum_roi_net_7d": 120.0,
        "mean_roi_net_7d": 2.4,
        "sum_neg_sq_roi_7d": 0.0,
        "median_entry_price_7d": None,
    }
    verdict = await evaluate_promote_readiness(
        "D_no_adv",
        promoted_at="2026-01-01",
        days_virtual=20,
        adversarial_pass=None,
        execution_enabled=True,
        period=period,
        recent=recent,
        virtual_metrics={"n_placed": 50, "n_filled": 48, "n_would_have_filled": 0},
    )
    assert verdict["decision"] == "MANUAL_OVERRIDE_NEEDED"


@pytest.mark.asyncio
async def test_missing_period_and_recent_defaults_to_empty():
    """detector без сигналов вообще (period/recent={}) → n_terminal=0 → INSUFFICIENT_DATA."""
    verdict = await evaluate_promote_readiness(
        "D_no_data",
        promoted_at=None,
        days_virtual=0,
        adversarial_pass=None,
        execution_enabled=False,
        period={},
        recent={},
        virtual_metrics={},
    )
    assert verdict["decision"] == "INSUFFICIENT_DATA"
    assert verdict["gates"]["n_min"]["value"] == 0


# ── fetch fallback path: caller не передаёт period/recent/virtual_metrics ─────


@pytest.mark.asyncio
async def test_fetches_internally_when_not_provided():
    """Без явных period/recent/virtual_metrics — evaluate_promote_readiness сама
    фетчит через fetch_virtual_period_signals/fetch_recent_window_signals/
    fetch_virtual_metrics_7d (single-detector caller path — TG promote callback)."""
    period_mock = AsyncMock(return_value={"D_x": {"terminal_7d": 35, "wins_7d": 25}})
    recent_mock = AsyncMock(
        return_value={
            "D_x": {
                "sum_roi_net_7d": 50.0,
                "mean_roi_net_7d": 1.5,
                "sum_neg_sq_roi_7d": 0.0,
                "median_entry_price_7d": None,
            }
        }
    )
    virtual_mock = AsyncMock(return_value={"D_x": {"n_placed": 35, "n_filled": 30, "n_would_have_filled": 0}})

    with (
        patch("app.services.detector_metrics.fetch_virtual_period_signals", period_mock),
        patch("app.services.detector_metrics.fetch_recent_window_signals", recent_mock),
        patch("app.services.detector_metrics.fetch_virtual_metrics_7d", virtual_mock),
    ):
        verdict = await evaluate_promote_readiness(
            "D_x",
            promoted_at="2026-01-01",
            days_virtual=20,
            adversarial_pass=True,
            execution_enabled=True,
        )

    period_mock.assert_called_once()
    recent_mock.assert_called_once()
    virtual_mock.assert_called_once()
    assert verdict["gates"]["n_min"]["value"] == 35
    assert verdict["decision"] == "READY"


# ── fetch_recent_window_signals edge cases ────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_recent_window_signals_empty_detectors_returns_empty():
    """Пустой список detectors → {} без похода в БД."""
    result = await fetch_recent_window_signals([])
    assert result == {}


@pytest.mark.asyncio
async def test_fetch_recent_window_signals_rejects_non_30_days():
    """days != 30 → ValueError (SQL INTERVAL literal фиксирован на 30д)."""
    with pytest.raises(ValueError, match="days=30"):
        await fetch_recent_window_signals(["D_x"], days=7)


@pytest.mark.asyncio
async def test_fetch_recent_window_signals_fills_missing_detectors_with_empty_dict():
    """Detector без строк в результате SQL → {} (drop-in shape с fetch_shadow_signals_7d)."""
    fetch_mock = AsyncMock(return_value=[])
    with patch("app.services.detector_metrics.fetch", fetch_mock):
        result = await fetch_recent_window_signals(["D_no_signals"])
    assert result == {"D_no_signals": {}}


# ── fetch_live_metrics_7d SQL sanity (текстовая проверка, без БД) ────────────


def test_live_metrics_sql_uses_maker_taker_not_virtual():
    sql = _LIVE_METRICS_7D_SQL.lower()
    assert "execution_mode in ('maker', 'taker')" in sql
    assert "= 'virtual'" not in sql


def test_live_metrics_sql_uses_unprefixed_statuses():
    """Live execution_orders.status — 'placed'/'filled'/'settled' (без virtual_ префикса)."""
    sql = _LIVE_METRICS_7D_SQL
    assert "'virtual_placed'" not in sql
    assert "'virtual_filled'" not in sql
    assert "'virtual_settled'" not in sql
    assert "'placed', 'filled', 'settled', 'rejected'" in sql


def test_live_metrics_sql_has_no_simulation_model_version_filter():
    """Live-ордера не имеют simulation_model_version (paper-only concept) — фильтр отсутствует."""
    assert "simulation_model_version" not in _LIVE_METRICS_7D_SQL
