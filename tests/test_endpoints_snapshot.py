"""Snapshot tests для 5 detector endpoints (Phase 2 safety net).

Захватывают current response shape **до** refactor consolidation. После
extract'а в app/services/detector_metrics.py — те же тесты должны pass
с identical responses.

5 endpoints under test:
  /status                            → bot_status
  /v2/detectors                      → bot_v2_detectors
  /v2/lifecycle/promotion_readiness  → bot_v2_lifecycle_promotion_readiness
  /v2/lifecycle/live_readiness       → bot_v2_lifecycle_live_readiness
  /v2/lifecycle/live_dashboard       → bot_v2_lifecycle_live_dashboard

Стратегия: подаём фиксированный mock data, проверяем shape ключей
+ representative values. Не fragile (не attach'имся к каждому числу), но
ловим breaking changes в API contract.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

# ─── Helpers ──────────────────────────────────────────────────────────────────


def _shape_keys(d: dict | list, max_depth: int = 3, depth: int = 0) -> dict | list:
    """Recursively extract just the SHAPE (keys + types) of a response, no values.

    Use to detect schema changes без assertion на конкретные numbers.
    """
    if depth >= max_depth:
        return type(d).__name__
    if isinstance(d, dict):
        return {k: _shape_keys(v, max_depth, depth + 1) for k, v in d.items()}
    if isinstance(d, list):
        if not d:
            return []
        return [_shape_keys(d[0], max_depth, depth + 1)]
    return type(d).__name__


# ─── /status ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_response_shape() -> None:
    """Frozen shape /status: status, execution_enabled, virtual_*, alerts."""
    from app.api.bot import bot_status

    fake_pnl_row = {"total_pnl": 123.45, "wins": 10, "losses": 5}
    fake_last_virtual = {"last_opened": datetime.now(UTC), "open_count": 7}

    async def mock_fetchrow(query, *args, **kwargs):
        if "open_count" in query or "pipeline_v2_shadow_positions" in query and "MAX(opened_at)" in query:
            return fake_last_virtual
        if "total_pnl" in query:
            return fake_pnl_row
        return fake_last_virtual

    with patch("app.api.bot.fetchrow", new=AsyncMock(side_effect=mock_fetchrow)):
        with patch("app.api.bot.get_settings") as mock_settings:
            mock_settings.return_value.execution_enabled = False
            result = await bot_status(_admin={})

    assert set(result.keys()) >= {
        "status", "execution_enabled", "virtual_pnl_usd", "virtual_resolved",
        "virtual_open", "virtual_wr", "last_virtual_at", "alerts",
    }
    assert result["status"] in ("live", "virtual")
    assert isinstance(result["alerts"], list)


# ─── /v2/detectors ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_v2_detectors_response_shape() -> None:
    """Frozen shape /v2/detectors: detectors[].{recent_14d, lifecycle{4 stages}, calibrator}."""
    from app.api.bot import bot_v2_detectors

    fake_historical = [
        {
            "detector": "D7b_no_favorite_fade", "n_total": 100, "n_resolved": 80,
            "n_wins": 12, "n_losses": 68, "n_expired": 20,
            "avg_roi_pct": -5.5, "avg_confidence": 0.72,
            "last_emit_at": datetime.now(UTC),
        }
    ]
    fake_recent = [
        {
            "detector": "D7b_no_favorite_fade", "n_total_recent": 30,
            "n_wins_recent": 5, "n_losses_recent": 25, "recent_roi_net_pct": 2.5,
        }
    ]
    fake_state = [{"detector": "D7b_no_favorite_fade", "stage": "virtual"}]
    fake_shadow = [{"detector": "D7b_no_favorite_fade", "signals_per_day_7d": 4.5}]
    fake_virt_live: list[dict] = []

    async def mock_fetch(query, *args, **kwargs):
        if "n_total" in query and "GROUP BY ds.detector" in query and "interval '14 days'" not in query and "interval '28 days'" not in query:
            return fake_historical
        if "interval '14 days'" in query and "interval '28 days'" not in query:
            return fake_recent
        if "interval '28 days'" in query:
            return []  # prev empty
        if "ml_calibrators" in query:
            return []
        if "detector_state" in query:
            return fake_state
        if "signals_per_day_7d" in query:
            return fake_shadow
        if "execution_orders" in query:
            return fake_virt_live
        return []

    mock = AsyncMock(side_effect=mock_fetch)
    with patch("app.api.bot.fetch", new=mock), \
         patch("app.services.detector_metrics.fetch", new=mock):
        result = await bot_v2_detectors(_admin={})

    # Shape
    assert "detectors" in result
    assert len(result["detectors"]) == 1
    d = result["detectors"][0]

    expected_top_keys = {
        "detector", "n_total", "n_resolved", "n_wins", "n_losses", "n_expired",
        "avg_roi_pct", "avg_confidence", "last_emit_at", "wr",
        "recent_14d", "calibrator", "lifecycle",
    }
    assert set(d.keys()) >= expected_top_keys, f"missing: {expected_top_keys - set(d.keys())}"

    # Recent 14d sub-shape
    assert set(d["recent_14d"].keys()) == {
        "n_total", "n_resolved", "n_wins", "n_losses", "wr", "roi_net_pct",
        "trend_pp", "verdict",
    }
    assert d["recent_14d"]["verdict"] in ("GREEN", "YELLOW", "RED")

    # Lifecycle 4-stage shape
    assert set(d["lifecycle"].keys()) == {"stage", "backtest", "shadow", "virtual", "live"}
    assert d["lifecycle"]["stage"] == "virtual"
    assert "verdict" in d["lifecycle"]["backtest"]
    assert "signals_per_day_7d" in d["lifecycle"]["shadow"]
    assert "pnl_usd_7d" in d["lifecycle"]["virtual"]
    assert "pnl_usd_7d" in d["lifecycle"]["live"]


# ─── /v2/lifecycle/promotion_readiness ────────────────────────────────────────


@pytest.mark.asyncio
async def test_promotion_readiness_response_shape() -> None:
    """Frozen shape: detectors[].{shadow_7d, gates{3 legacy}, gates_v2{5 honest v3}}.

    Honest v3 gates (2026-05-27 после deep-research):
    research/shadow-gates-methodology-2026-05/
    """
    from app.api.bot import bot_v2_lifecycle_promotion_readiness

    fake_signals = [
        {
            "detector": "D7b_no_favorite_fade",
            "distinct_events_7d": 50,
            "distinct_markets_7d": 50,
            "wins_7d": 8,
            "terminal_7d": 50,
            "resolved_7d": 50,
            "total_7d": 100,
            "sum_roi_net_7d": 0.5,
            "mean_roi_net_7d": 0.01,
            "sum_neg_sq_roi_7d": 0.5,
            "median_entry_price_7d": 0.10,
            "catastrophic_losses_7d": 1,
        }
    ]
    fake_state = [{"detector": "D7b_no_favorite_fade", "stage": "shadow"}]

    async def mock_fetch(query, *args, **kwargs):
        if "detector_signals" in query:
            return fake_signals
        if "detector_state" in query:
            return fake_state
        if "execution_orders" in query:
            return []  # no virtual fingerprint
        return []

    with patch("app.services.detector_metrics.fetch", new=AsyncMock(side_effect=mock_fetch)):
        result = await bot_v2_lifecycle_promotion_readiness(_admin={})

    assert "detectors" in result
    if result["detectors"]:
        d = result["detectors"][0]
        expected_keys = {
            "detector",
            "stage",
            "shadow_7d",
            "canonical",
            "gates",
            "ready",
            "gates_passed",
            "gates_total",
            "gates_v2",
            "ready_v2",
            "gates_v2_passed",
            "gates_v2_total",
        }
        assert set(d.keys()) >= expected_keys, f"missing: {expected_keys - set(d.keys())}"
        assert d["gates_total"] == 3
        assert {"n_events", "wr_match", "verdict"}.issubset(set(d["gates"].keys()))
        assert d["gates_v2_total"] == 5
        assert {"sample", "net_pnl", "tail_risk", "break_even_wr", "sortino"} == set(
            d["gates_v2"].keys()
        )


# ─── /v2/lifecycle/live_readiness ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_live_readiness_response_shape() -> None:
    """Frozen shape: detectors[].{virtual_7d, gates{5 gates}, settle_velocity, ready}."""
    from app.api.bot import bot_v2_lifecycle_live_readiness

    promoted_22d_ago = datetime.now(UTC) - timedelta(days=22)
    fake_state = [{"detector": "D7b_no_favorite_fade", "stage": "virtual", "promoted_at": promoted_22d_ago}]
    fake_virtual = [
        {
            "detector": "D7b_no_favorite_fade", "n_placed": 72, "n_filled": 45,
            "n_settled": 28, "wins": 18, "losses": 10, "pnl_usd": 45.20,
        }
    ]

    fake_portfolios = [
        {
            "portfolio_id": "rules", "display_name": "Rules", "bankroll_usd": 3000.0,
            "exposure_cap_usd": 3000.0, "kelly_fraction": 0.20, "gate_overrides": {}, "enabled": True,
        }
    ]
    fake_portfolio_map = [{"detector": "D7b_no_favorite_fade", "portfolio_id": "rules"}]

    async def mock_fetch(query, *args, **kwargs):
        if "hours_to_settle" in query:
            return []
        if "execution_orders" in query:
            return fake_virtual
        if "detector_signals" in query:
            return []
        if "FROM portfolios" in query:
            return fake_portfolios
        if "WHERE portfolio_id IS NOT NULL" in query:
            return fake_portfolio_map
        return fake_state

    mock = AsyncMock(side_effect=mock_fetch)
    # Phase 4: invalidate portfolio cache между тестами + patch portfolios.fetch
    from app.services import portfolios as portfolios_svc
    portfolios_svc.invalidate_cache()
    with patch("app.api.bot.fetch", new=mock), \
         patch("app.services.detector_metrics.fetch", new=mock), \
         patch("app.services.portfolios.fetch", new=mock), \
         patch("app.api.bot.get_settings") as mock_settings:
        mock_settings.return_value.execution_enabled = False
        result = await bot_v2_lifecycle_live_readiness(_admin={})

    expected_top_keys = {
        "detectors",
        "candidates_from_shadow",
        "execution_enabled",
        "settle_velocity_global",
        # Phase 0 Stage 5 — PnL-weighted holding period (additive)
        "settle_velocity_global_pnl_weighted",
    }
    assert set(result.keys()) == expected_top_keys

    assert len(result["detectors"]) == 1
    d = result["detectors"][0]
    # Phase 4: additive — portfolio_id, gate_overrides_applied добавлены.
    # 2026-05-29: gates_v2 + v2-counts (UI/карточка судит по signal-level v2).
    expected_d_keys = {
        "detector", "stage", "promoted_at", "days_virtual",
        "virtual_7d", "canonical", "gates", "ready", "gates_passed",
        "gates_total", "settle_velocity",
        "portfolio_id", "gate_overrides_applied",
        "gates_v2", "gates_decision_v2", "ready_v2",
        "gates_v2_passed", "gates_v2_total",
    }
    assert set(d.keys()) >= expected_d_keys, f"missing: {expected_d_keys - set(d.keys())}"

    # 5 legacy gates structure (backward-compat)
    expected_gates = {
        "pnl_positive", "fill_rate", "adversarial_mirror",
        "days_accumulated", "execution_enabled",
    }
    assert set(d["gates"].keys()) == expected_gates
    assert d["gates_total"] == 5

    # 6 honest gates_v2 structure (evaluate_promote_gates) — SSoT для UI.
    # Ровно то, по чему судит TG promote virtual→live (telegram.py).
    expected_gates_v2 = {
        "n_min", "pnl_positive", "fill_rate",
        "adversarial_mirror", "days_accumulated", "execution_enabled",
    }
    assert set(d["gates_v2"].keys()) == expected_gates_v2
    assert d["gates_v2_total"] == 6
    assert isinstance(d["gates_v2_passed"], int)
    assert isinstance(d["ready_v2"], bool)
    # ready_v2 = decision == "READY" (hard AND, не ≥threshold).
    assert d["ready_v2"] == (d["gates_decision_v2"] == "READY")
    assert d["gates_decision_v2"] in {
        "READY", "INSUFFICIENT_DATA", "BLOCKED", "MANUAL_OVERRIDE_NEEDED",
    }

    # Virtual 7d sub-shape
    assert set(d["virtual_7d"].keys()) >= {
        "n_placed", "n_filled", "n_settled", "fill_rate_pct",
        "pnl_usd", "wins", "losses", "wr_pct",
    }

    # Phase 4 assertions
    assert d["portfolio_id"] == "rules"
    assert d["gate_overrides_applied"] == []  # rules portfolio has no overrides


# ─── /v2/portfolios (Phase 4) ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_v2_portfolios_response_shape() -> None:
    """Frozen shape: portfolios[].{portfolio_id, bankroll, cap, used, remaining, kelly, gate_overrides, n_detectors, enabled}."""
    from app.api.bot import bot_v2_portfolios

    fake_portfolios = [
        {
            "portfolio_id": "rules", "display_name": "Rules-based",
            "bankroll_usd": 3000.0, "exposure_cap_usd": 3000.0,
            "kelly_fraction": 0.20, "gate_overrides": {}, "enabled": True,
        },
        {
            "portfolio_id": "ml", "display_name": "ML model",
            "bankroll_usd": 1000.0, "exposure_cap_usd": 1000.0,
            "kelly_fraction": 0.10,
            "gate_overrides": {"adversarial_mirror": "skip"},
            "enabled": True,
        },
    ]

    async def mock_fetch(query, *args, **kwargs):
        if "FROM portfolios" in query:
            return fake_portfolios
        return []

    async def mock_fetchrow(query, *args, **kwargs):
        # mock used + n_detectors row
        if "rules" in args:
            return {"used": 1500.0, "n_detectors": 6}
        return {"used": 0.0, "n_detectors": 1}

    from app.services import portfolios as portfolios_svc
    portfolios_svc.invalidate_cache()
    with patch("app.services.portfolios.fetch", new=AsyncMock(side_effect=mock_fetch)), \
         patch("app.api.bot.fetchrow", new=AsyncMock(side_effect=mock_fetchrow)):
        result = await bot_v2_portfolios(_admin={})

    assert "portfolios" in result
    assert len(result["portfolios"]) == 2

    expected_keys = {
        "portfolio_id", "display_name",
        "bankroll_usd", "exposure_cap_usd",
        "exposure_used_usd", "exposure_remaining_usd",
        "kelly_fraction", "gate_overrides",
        "n_detectors", "enabled",
    }
    for p in result["portfolios"]:
        assert set(p.keys()) == expected_keys, f"unexpected keys: {set(p.keys()) ^ expected_keys}"

    # Sanity: rules portfolio
    rules = next(p for p in result["portfolios"] if p["portfolio_id"] == "rules")
    assert rules["bankroll_usd"] == 3000.0
    assert rules["exposure_used_usd"] == 1500.0
    assert rules["exposure_remaining_usd"] == 1500.0
    assert rules["n_detectors"] == 6


# ─── /v2/lifecycle/live_dashboard ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_live_dashboard_response_shape() -> None:
    """Frozen shape: bankroll, equity_curve_7d, alerts, open_positions, fill_quality_7d, detectors, execution_enabled."""
    from app.api.bot import bot_v2_lifecycle_live_dashboard

    async def mock_fetch(query, *args, **kwargs):
        return []  # empty data — focus только на shape

    async def mock_fetchrow(query, *args, **kwargs):
        return None

    with patch("app.api.bot.fetch", new=AsyncMock(side_effect=mock_fetch)):
        with patch("app.api.bot.fetchrow", new=AsyncMock(side_effect=mock_fetchrow)):
            with patch("app.api.bot.get_settings") as mock_settings:
                mock_settings.return_value.execution_enabled = False
                result = await bot_v2_lifecycle_live_dashboard(_admin={})

    expected_top_keys = {
        "bankroll", "equity_curve_7d", "alerts", "open_positions",
        "fill_quality_7d", "detectors", "execution_enabled",
    }
    assert set(result.keys()) == expected_top_keys, f"diff: {expected_top_keys ^ set(result.keys())}"
    assert isinstance(result["alerts"], list)
    assert isinstance(result["detectors"], list)
