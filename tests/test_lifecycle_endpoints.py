"""Tests for /v2/lifecycle/counts and /v2/detectors/{detector}/buckets endpoints.

Эндпойнты обслуживают новый 4-stage UI dashboard в /bot. См.
docs/detector_lifecycle.md и plans/wiggly-nibbling-stardust.md.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_lifecycle_counts_combines_sources():
    """Эндпойнт собирает counts из 3 источников: backtest MD, signals, detector_state."""
    from app.api.bot import bot_v2_lifecycle_counts

    with (
        patch("app.api.bot.fetchrow", new=AsyncMock(return_value={"n": 6})),
        patch(
            "app.api.bot.fetch",
            new=AsyncMock(return_value=[{"stage": "shadow", "n": 5}, {"stage": "live", "n": 1}]),
        ),
        patch(
            "app.services.backtest_metadata.list_available_backtests",
            return_value=["D10b_election_ladder", "D10b_election_ladder", "STANDALONE_V2_LGB"],
        ),
    ):
        result = await bot_v2_lifecycle_counts(_admin={"role": "admin"})

    assert result["backtest"]["total"] == 3
    assert result["shadow"]["total"] == 6  # из fetchrow
    assert result["virtual"]["total"] == 0  # отсутствует в state_rows
    assert result["live"]["total"] == 1
    for stage in ("backtest", "shadow", "virtual", "live"):
        assert "label" in result[stage]


@pytest.mark.asyncio
async def test_detector_buckets_aggregates_per_category_zone():
    """SQL результат → структурированный JSON с roi и wr per-bucket."""
    from app.api.bot import bot_v2_detector_buckets

    fake_rows = [
        {
            "category": "Sports",
            "zone": "60-80",
            "n_signals": 42,
            "n_resolved": 38,
            "n_wins": 27,
            "avg_roi": 0.352,
            "virtual_pnl": 12.5,
            "live_pnl": 0,
        },
        {
            "category": "Politics",
            "zone": "lt40",
            "n_signals": 5,
            "n_resolved": 0,
            "n_wins": 0,
            "avg_roi": None,
            "virtual_pnl": 0,
            "live_pnl": 0,
        },
    ]

    with patch("app.api.bot.fetch", new=AsyncMock(return_value=fake_rows)):
        result = await bot_v2_detector_buckets(detector="D10b_election_ladder", _admin={})

    assert result["detector"] == "D10b_election_ladder"
    assert result["window_days"] == 7
    assert len(result["buckets"]) == 2

    sports = result["buckets"][0]
    assert sports["category"] == "Sports"
    assert sports["zone"] == "60-80"
    assert sports["n_signals_7d"] == 42
    assert sports["n_resolved_7d"] == 38
    assert sports["wr_pct"] == 71.1  # 27/38 * 100, округление до 1
    assert sports["roi_net_pct"] == 0.4  # 0.352 → round to 0.4 (1 decimal)
    assert sports["virtual_pnl_7d"] == 12.5
    assert sports["live_pnl_7d"] == 0.0

    politics = result["buckets"][1]
    assert politics["wr_pct"] is None  # n_resolved=0 → None
    assert politics["roi_net_pct"] is None


@pytest.mark.asyncio
async def test_detector_buckets_empty_returns_empty_list():
    """Детектор без signals → пустой список."""
    from app.api.bot import bot_v2_detector_buckets

    with patch("app.api.bot.fetch", new=AsyncMock(return_value=[])):
        result = await bot_v2_detector_buckets(detector="D99_unknown", _admin={})

    assert result["buckets"] == []
    assert result["detector"] == "D99_unknown"


@pytest.mark.asyncio
async def test_lifecycle_counts_handles_empty_db():
    """Свежая БД без detector_state → counts virtual/live = 0."""
    from app.api.bot import bot_v2_lifecycle_counts

    with (
        patch("app.api.bot.fetchrow", new=AsyncMock(return_value={"n": 0})),
        patch("app.api.bot.fetch", new=AsyncMock(return_value=[])),
        patch(
            "app.services.backtest_metadata.list_available_backtests",
            return_value=[],
        ),
    ):
        result = await bot_v2_lifecycle_counts(_admin={})

    assert result["backtest"]["total"] == 0
    assert result["shadow"]["total"] == 0
    assert result["virtual"]["total"] == 0
    assert result["live"]["total"] == 0


@pytest.mark.asyncio
async def test_backtest_endpoint_includes_parsed_sections():
    """Эндпойнт /v2/detectors/{det}/backtest парсит body sections."""
    from app.api.bot import bot_v2_detector_backtest

    # D10b есть в docs/backtests/ — реальный parse
    result = await bot_v2_detector_backtest(detector="D10b_election_ladder", _admin={})

    assert result["available"] is True
    assert "sections" in result
    sections = result["sections"]
    # Hypothesis парсится как первый параграф
    assert "hypothesis" in sections
    assert len(sections["hypothesis"]) > 50


@pytest.mark.asyncio
async def test_backtest_endpoint_returns_unavailable_for_missing():
    from app.api.bot import bot_v2_detector_backtest

    result = await bot_v2_detector_backtest(detector="D99_nonexistent", _admin={})

    assert result["available"] is False
    assert "sections" not in result


@pytest.mark.asyncio
async def test_promotion_readiness_evaluates_3_gates():
    """3 gates: n_events ≥ 30, WR ±5pp от canonical, verdict=PASS."""
    from app.api.bot import bot_v2_lifecycle_promotion_readiness

    # Mock: D10b с 50 events → все 3 gates PASS
    fake_shadow_rows = [
        {
            "detector": "D10b_election_ladder",
            "distinct_events_7d": 50,
            "distinct_markets_7d": 50,
            # 18/48 = 37.5% ≈ canonical wr_pct 37.4 (docs/backtests/D10b_election_ladder.md)
            # → Gate 2 (wr_match ±5pp) PASS.
            "wins_7d": 18,
            "terminal_7d": 48,
            "resolved_7d": 48,
            "total_7d": 60,
        },
        # D99 с 10 events (below threshold) — gate 1 FAIL
        {
            "detector": "D99_new_unproven",
            "distinct_events_7d": 10,
            "distinct_markets_7d": 10,
            "wins_7d": 4,
            "terminal_7d": 8,
            "resolved_7d": 8,
            "total_7d": 12,
        },
    ]
    fake_state = [
        {"detector": "D10b_election_ladder", "stage": "shadow"},
        {"detector": "D99_new_unproven", "stage": "shadow"},
    ]

    async def mock_fetch(query, *args, **kwargs):
        # fetch_virtual_fingerprints() (v2 gates) дёргает execution_orders —
        # для этого теста нет virtual данных, возвращаем пусто → stylized fallback.
        if "execution_orders" in query:
            return []
        if "detector_signals" in query:
            return fake_shadow_rows
        return fake_state

    mock = AsyncMock(side_effect=mock_fetch)
    with patch("app.api.bot.fetch", new=mock), \
         patch("app.services.detector_metrics.fetch", new=mock):
        result = await bot_v2_lifecycle_promotion_readiness(_admin={})

    detectors = result["detectors"]
    assert len(detectors) >= 2

    d10b = next(d for d in detectors if d["detector"] == "D10b_election_ladder")
    # Gate 1: 50 ≥ 30 → PASS
    assert d10b["gates"]["n_events"]["passed"] is True
    # Gate 3: D10b verdict = PASS (читаем реальный MD)
    assert d10b["gates"]["verdict"]["passed"] is True
    # Gate 2: 8/48 = 16.7% vs canonical → delta ≤ 5pp → PASS
    assert d10b["gates"]["wr_match"]["passed"] is True
    assert d10b["ready"] is True

    d99 = next(d for d in detectors if d["detector"] == "D99_new_unproven")
    # Gate 1: 10 < 30 → FAIL
    assert d99["gates"]["n_events"]["passed"] is False
    # Backtest doc нет → gate 3 FAIL
    assert d99["gates"]["verdict"]["passed"] is False
    assert d99["ready"] is False


@pytest.mark.asyncio
async def test_live_readiness_evaluates_5_gates():
    """5 Virtual → Live gates: pnl > 0, fill_rate ≥ 50%, adv mirror, 14+d, EXEC enabled."""
    from datetime import UTC, datetime, timedelta

    from app.api.bot import bot_v2_lifecycle_live_readiness

    promoted_22d_ago = datetime.now(UTC) - timedelta(days=22)

    fake_state = [{"detector": "D10b_election_ladder", "stage": "virtual", "promoted_at": promoted_22d_ago}]
    fake_virtual = [
        {
            "detector": "D10b_election_ladder",
            "n_placed": 72,
            "n_filled": 45,
            "n_settled": 28,
            "wins": 18,
            "losses": 10,
            "pnl_usd": 45.20,
        }
    ]

    async def mock_fetch(query, *args, **kwargs):
        if "hours_to_settle" in query:
            return []  # no settle velocity data for this test
        if "execution_orders" in query:
            return fake_virtual
        if "detector_signals" in query:
            return []  # no shadow candidates needed
        if "FROM portfolios" in query:
            return []  # Phase 4: no portfolios → portfolio_id=None
        if "WHERE portfolio_id IS NOT NULL" in query:
            return []
        return fake_state

    mock = AsyncMock(side_effect=mock_fetch)
    from app.services import portfolios as portfolios_svc
    portfolios_svc.invalidate_cache()
    with patch("app.api.bot.fetch", new=mock), \
         patch("app.services.detector_metrics.fetch", new=mock), \
         patch("app.services.portfolios.fetch", new=mock), \
         patch("app.api.bot.get_settings") as mock_settings:
        mock_settings.return_value.execution_enabled = False
        result = await bot_v2_lifecycle_live_readiness(_admin={})

    assert "detectors" in result
    assert "candidates_from_shadow" in result
    assert result["execution_enabled"] is False
    assert "settle_velocity_global" in result
    d7b_velocity = next(
        d["settle_velocity"] for d in result["detectors"] if d["detector"] == "D10b_election_ladder"
    )
    assert d7b_velocity["n"] == 0
    assert d7b_velocity["buckets"] is None

    d7b = next(d for d in result["detectors"] if d["detector"] == "D10b_election_ladder")
    # Gate 1 PASS: pnl > 0 (45.20)
    assert d7b["gates"]["pnl_positive"]["passed"] is True
    # Gate 2 PASS: 45/72 = 62.5% ≥ 50%
    assert d7b["gates"]["fill_rate"]["passed"] is True
    assert d7b["gates"]["fill_rate"]["value"] == 62.5
    # Gate 4 PASS: 22d ≥ 14
    assert d7b["gates"]["days_accumulated"]["passed"] is True
    # Gate 5 FAIL: EXECUTION_ENABLED=false
    assert d7b["gates"]["execution_enabled"]["passed"] is False
    # 4/5 gates passed (G3 adv: D10b backtest md имеет adversarial_mirror_pass=false → fail)
    # Поскольку D10b.md имеет adversarial_mirror_pass: false → gate_adv = False
    assert d7b["gates"]["adversarial_mirror"]["passed"] is False
    # gates_passed: G1, G2, G4 = 3 (G3 false, G5 false)
    assert d7b["gates_passed"] == 3
    assert d7b["ready"] is False


@pytest.mark.asyncio
async def test_live_readiness_includes_shadow_candidates():
    """candidates_from_shadow = детекторы с Shadow 3/3 gates passed, НЕ в virtual."""
    from app.api.bot import bot_v2_lifecycle_live_readiness

    # No virtual detectors, but D10b has 50 events 7d → passes shadow gates
    async def mock_fetch(query, *args, **kwargs):
        if "execution_orders" in query:
            return []
        if "detector_signals" in query:
            return [
                {
                    "detector": "D10b_election_ladder",
                    "distinct_events_7d": 50,
                    # 18/48 = 37.5% ≈ canonical wr_pct 37.4 → Gate 2 PASS, попадает в candidates.
                    "wins_7d": 18,
                    "terminal_7d": 48,
                }
            ]
        if "FROM portfolios" in query:
            return []
        if "WHERE portfolio_id IS NOT NULL" in query:
            return []
        return []  # no virtual state

    mock = AsyncMock(side_effect=mock_fetch)
    from app.services import portfolios as portfolios_svc
    portfolios_svc.invalidate_cache()
    with patch("app.api.bot.fetch", new=mock), \
         patch("app.services.detector_metrics.fetch", new=mock), \
         patch("app.services.portfolios.fetch", new=mock), \
         patch("app.api.bot.get_settings") as mock_settings:
        mock_settings.return_value.execution_enabled = True
        result = await bot_v2_lifecycle_live_readiness(_admin={})

    assert result["detectors"] == []
    candidates = result["candidates_from_shadow"]
    assert len(candidates) == 1
    assert candidates[0]["detector"] == "D10b_election_ladder"
    assert candidates[0]["shadow_gates_passed"] == 3


@pytest.mark.asyncio
async def test_promotion_readiness_handles_empty_shadow():
    """Свежий детектор без shadow signals: все gates FAIL."""
    from app.api.bot import bot_v2_lifecycle_promotion_readiness

    async def mock_fetch(query, *args, **kwargs):
        if "execution_orders" in query:
            return []  # fetch_virtual_fingerprints — нет virtual данных
        if "detector_signals" in query:
            return []
        return [{"detector": "D99_new", "stage": "shadow"}]

    mock = AsyncMock(side_effect=mock_fetch)
    with patch("app.api.bot.fetch", new=mock), \
         patch("app.services.detector_metrics.fetch", new=mock):
        result = await bot_v2_lifecycle_promotion_readiness(_admin={})

    d99 = next(d for d in result["detectors"] if d["detector"] == "D99_new")
    assert d99["shadow_7d"]["total_signals"] == 0
    assert d99["gates"]["n_events"]["passed"] is False
    assert d99["ready"] is False


@pytest.mark.asyncio
async def test_backtest_map_includes_all_detectors_with_md():
    """Backtest map endpoint возвращает все детекторы с MD-файлами + their stage."""
    from app.api.bot import bot_v2_lifecycle_backtest_map

    fake_state = [
        {"detector": "D10b_election_ladder", "stage": "shadow"},
        {"detector": "D3_cross_mkt_arb", "stage": "shadow"},
    ]
    with patch("app.api.bot.fetch", new=AsyncMock(return_value=fake_state)):
        result = await bot_v2_lifecycle_backtest_map(_admin={})

    assert "items" in result
    assert len(result["items"]) >= 2  # минимум D10b + D3
    detectors_in = {item["detector"] for item in result["items"]}
    assert "D10b_election_ladder" in detectors_in
    # D10b должен иметь stage='shadow' из mock
    d10b = next(i for i in result["items"] if i["detector"] == "D10b_election_ladder")
    assert d10b["stage"] == "shadow"
    assert d10b["verdict"] == "PASS"
    assert isinstance(d10b["honest_daily_usd_at_5k"], (int, float))
    assert isinstance(d10b["honest_haircut_factor"], (int, float))


@pytest.mark.asyncio
async def test_live_dashboard_aggregates_bankroll_and_alerts():
    """live_dashboard возвращает bankroll, equity_curve, alerts, fill_quality, detectors."""
    from app.api.bot import bot_v2_lifecycle_live_dashboard

    async def mock_fetch(query, *args, **kwargs):
        # Equity curve daily breakdown
        if "GROUP BY day, execution_mode" in query:
            return []
        # detector_state stage='live' — none
        if "detector_state" in query and "stage = 'live'" in query:
            return []
        # Open positions
        if "outcome IS NULL" in query:
            return []
        return []

    async def mock_fetchrow(query, *args, **kwargs):
        if "strategy_bankroll" in query:
            return {"initial_bankroll": 5000.0, "bankroll": 4900.0}
        # Fill quality 7d
        if "FROM execution_orders" in query and "INTERVAL '7 days'" in query:
            return {
                "n_placed": 0,
                "n_filled": 0,
                "slippage_total": 0,
                "fee_total": 0,
                "pnl_total": 0,
            }
        return None

    with (
        patch("app.api.bot.fetch", new=AsyncMock(side_effect=mock_fetch)),
        patch("app.api.bot.fetchrow", new=AsyncMock(side_effect=mock_fetchrow)),
        patch("app.api.bot.get_settings") as mock_settings,
        patch(
            "app.services.wallet_resolver.fetch_balance",
            new=AsyncMock(side_effect=Exception("offline")),
        ),
    ):
        mock_settings.return_value.execution_enabled = False
        result = await bot_v2_lifecycle_live_dashboard(_admin={})

    # Bankroll
    assert result["bankroll"]["initial_bankroll_usd"] == 5000.0
    # wallet недоступен → fallback к db_bankroll = 4900
    assert result["bankroll"]["wallet_balance_usd"] == 4900.0
    assert result["bankroll"]["drawdown_pct"] == 2.0  # (1 − 4900/5000) × 100
    assert result["bankroll"]["wallet_source"] == "db"

    # Alerts: EXECUTION_ENABLED=false → warning + WALLET_UNAVAILABLE info + DRAWDOWN_MID
    alert_codes = {a["code"] for a in result["alerts"]}
    assert "EXEC_DISABLED" in alert_codes
    assert "WALLET_UNAVAILABLE" in alert_codes
    assert "DRAWDOWN_MID" in alert_codes  # 2.0% попадает в warning bucket

    # Equity curve: 7 точек, все нули
    assert len(result["equity_curve_7d"]) == 7
    assert all(p["live_pnl_cum"] == 0.0 for p in result["equity_curve_7d"])

    # Fill quality
    assert result["fill_quality_7d"]["n_placed"] == 0
    assert result["fill_quality_7d"]["fill_rate_pct"] is None

    # 0 live detectors → пустой список
    assert result["detectors"] == []
    assert result["execution_enabled"] is False


@pytest.mark.asyncio
async def test_live_dashboard_aggregates_open_positions_and_drawdown():
    """Critical drawdown >5% поднимает critical alert; open_positions посчитаны."""
    from datetime import UTC, datetime

    from app.api.bot import bot_v2_lifecycle_live_dashboard

    fake_open = [
        {
            "detector": "D10b_election_ladder",
            "market_id": "0x123",
            "side": "NO",
            "fill_price": 0.92,
            "stake": 50.0,
            "calibrated_p": 0.93,
            "created_at": datetime.now(UTC),
        }
    ]

    async def mock_fetch(query, *args, **kwargs):
        if "GROUP BY day, execution_mode" in query:
            return []
        if "detector_state" in query and "stage = 'live'" in query:
            return [{"detector": "D10b_election_ladder"}]
        if "outcome IS NULL" in query:
            return fake_open
        # Per-detector live aggregation
        if "GROUP BY detector" in query and "INTERVAL '7 days'" in query:
            return [
                {
                    "detector": "D10b_election_ladder",
                    "n_placed": 5,
                    "n_filled": 4,
                    "wins": 1,
                    "losses": 1,
                    "pnl_usd": -25.0,
                    "slippage_total": -1.5,
                    "fee_total": 0.8,
                }
            ]
        return []

    async def mock_fetchrow(query, *args, **kwargs):
        if "strategy_bankroll" in query:
            return {"initial_bankroll": 5000.0, "bankroll": 4500.0}
        if "FROM execution_orders" in query and "INTERVAL '7 days'" in query:
            return {
                "n_placed": 5,
                "n_filled": 4,
                "slippage_total": -1.5,
                "fee_total": 0.8,
                "pnl_total": -25.0,
            }
        return None

    with (
        patch("app.api.bot.fetch", new=AsyncMock(side_effect=mock_fetch)),
        patch("app.api.bot.fetchrow", new=AsyncMock(side_effect=mock_fetchrow)),
        patch("app.api.bot.get_settings") as mock_settings,
        patch(
            "app.services.wallet_resolver.fetch_balance",
            new=AsyncMock(return_value=4500.0),
        ),
    ):
        mock_settings.return_value.execution_enabled = True
        result = await bot_v2_lifecycle_live_dashboard(_admin={})

    # Drawdown 10% → critical
    assert result["bankroll"]["drawdown_pct"] == 10.0
    assert result["bankroll"]["wallet_source"] == "wallet"
    alert_codes = {a["code"] for a in result["alerts"]}
    assert "DRAWDOWN_HIGH" in alert_codes
    assert "EXEC_DISABLED" not in alert_codes  # exec enabled

    # Open positions
    assert result["open_positions"]["count"] == 1
    assert result["open_positions"]["total_exposure_usd"] == 50.0
    assert result["open_positions"]["list"][0]["detector"] == "D10b_election_ladder"

    # Per-detector
    assert len(result["detectors"]) == 1
    d7b = result["detectors"][0]
    assert d7b["live_7d"]["pnl_usd"] == -25.0
    assert d7b["live_7d"]["fill_rate_pct"] == 80.0  # 4/5
