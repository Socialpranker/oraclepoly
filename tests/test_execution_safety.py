"""Pure-логика execution_safety: signal age, drift threshold, decide_mode."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.core.horizon_limits import MAX_HORIZON_DAYS
from app.services.execution_safety import (
    LIVE_PRICE_DRIFT_MAX,
    LIVE_PRICE_DRIFT_MAX_LONGSHOT,
    LIVE_PRICE_DRIFT_MAX_MIDLOW,
    LIVE_SIGNAL_HARD_MAX_AGE_SEC,
    SafetyContext,
    check_balance,
    check_bucket_disabled,
    check_daemon_pause,
    check_horizon_cap,
    check_market_book,
    check_max_inflight_orders,
    check_max_open_positions,
    check_signal_age,
    decide_mode,
    evaluate,
    zone_drift_threshold,
)


def test_zone_drift_threshold_longshot():
    assert zone_drift_threshold(0.05) == LIVE_PRICE_DRIFT_MAX_LONGSHOT
    assert zone_drift_threshold(0.29) == LIVE_PRICE_DRIFT_MAX_LONGSHOT


def test_zone_drift_threshold_midlow():
    assert zone_drift_threshold(0.30) == LIVE_PRICE_DRIFT_MAX_MIDLOW
    assert zone_drift_threshold(0.49) == LIVE_PRICE_DRIFT_MAX_MIDLOW


def test_zone_drift_threshold_high():
    assert zone_drift_threshold(0.50) == LIVE_PRICE_DRIFT_MAX
    assert zone_drift_threshold(0.95) == LIVE_PRICE_DRIFT_MAX


def test_signal_age_none_returns_ok():
    ok, reason = check_signal_age(None)
    assert ok is True
    assert reason == ""


def test_signal_age_fresh():
    fresh = datetime.now(UTC) - timedelta(seconds=10)
    ok, _ = check_signal_age(fresh)
    assert ok is True


def test_signal_age_stale():
    stale = datetime.now(UTC) - timedelta(seconds=LIVE_SIGNAL_HARD_MAX_AGE_SEC + 30)
    ok, reason = check_signal_age(stale)
    assert ok is False
    assert "stale_signal" in reason


def test_signal_age_naive_datetime_treated_as_utc():
    """Naive datetime интерпретируется как UTC (как в legacy execution_job)."""
    fresh = datetime.utcnow() - timedelta(seconds=10)
    ok, _ = check_signal_age(fresh)
    assert ok is True


def test_signal_age_within_hard_cap_passes():
    """1799s < 1800s cap — сигнал в окне, судьбу решает drift-гейт дальше."""
    generated = datetime.now(UTC) - timedelta(seconds=1799)
    ok, reason = check_signal_age(generated)
    assert ok is True
    assert reason == ""


def test_signal_age_beyond_hard_cap_rejected():
    """1801s > 1800s cap — зомби-сигнал, терминальный reject."""
    generated = datetime.now(UTC) - timedelta(seconds=1801)
    ok, reason = check_signal_age(generated)
    assert ok is False
    assert reason.startswith("stale_signal:")
    assert "1800s" in reason


def test_signal_age_former_300s_now_passes():
    """Регресс anti-selection: 400s раньше резался (300s), теперь проходит."""
    generated = datetime.now(UTC) - timedelta(seconds=400)
    ok, reason = check_signal_age(generated)
    assert ok is True


def test_check_balance_ok():
    ok, reason = check_balance(stake_usd=10.0, bankroll_usd=100.0)
    assert ok is True
    assert reason == ""


def test_check_balance_insufficient():
    # buffer = $10 → нужно >= $20
    ok, reason = check_balance(stake_usd=10.0, bankroll_usd=15.0)
    assert ok is False
    assert "insufficient_balance" in reason


# ─── Placed/filled cap split (audit топ-5) ───


@pytest.mark.asyncio
async def test_max_inflight_counts_only_placed():
    """3 filled-позиции НЕ блокируют новый ордер (audit топ-5)."""
    with patch(
        "app.services.execution_safety.fetchval", new_callable=AsyncMock, return_value=0
    ):
        ok, reason = await check_max_inflight_orders()
    assert ok is True
    assert reason == ""


@pytest.mark.asyncio
async def test_max_open_positions_blocks_at_cap():
    with patch(
        "app.services.execution_safety.fetchval", new_callable=AsyncMock, return_value=20
    ):
        ok, reason = await check_max_open_positions()
    assert ok is False
    assert reason == "max_open_positions:20>=20"


@pytest.mark.asyncio
async def test_max_open_positions_allows_below_cap():
    """10 held-позиций (старый кап) больше не блокируют — кап поднят до 20 (2026-07-07)."""
    with patch(
        "app.services.execution_safety.fetchval", new_callable=AsyncMock, return_value=10
    ):
        ok, reason = await check_max_open_positions()
    assert ok is True
    assert reason == ""


# ─── check_daemon_pause (Task 4, audit-fixes phase1) ───


@pytest.mark.asyncio
async def test_daemon_pause_blocks_paused_detector():
    """Детектор в bot:daemon_paused_detectors → блокируем только его."""
    mock_redis = AsyncMock()
    mock_redis.sismember = AsyncMock(return_value=True)
    with patch("app.core.cache.get_redis", new_callable=AsyncMock, return_value=mock_redis):
        ok, reason = await check_daemon_pause("D_noisy")
    assert ok is False
    assert reason == "daemon_paused:D_noisy"


@pytest.mark.asyncio
async def test_daemon_pause_allows_unpaused_detector():
    """Детектора нет в set → ok."""
    mock_redis = AsyncMock()
    mock_redis.sismember = AsyncMock(return_value=False)
    with patch("app.core.cache.get_redis", new_callable=AsyncMock, return_value=mock_redis):
        ok, reason = await check_daemon_pause("D_clean")
    assert ok is True
    assert reason == ""


@pytest.mark.asyncio
async def test_daemon_pause_ok_when_redis_down():
    """Redis недоступен → не блокируем (conservative fail-open, как остальные Redis-checks)."""
    with patch("app.core.cache.get_redis", new_callable=AsyncMock, return_value=None):
        ok, reason = await check_daemon_pause("D_any")
    assert ok is True
    assert reason == ""


def test_decide_mode_thin_book_takes():
    assert decide_mode(book_depth_usd=50.0, signal_age_sec=10, ev_pct=8.0) == "taker"


def test_decide_mode_old_signal_takes():
    assert decide_mode(book_depth_usd=500.0, signal_age_sec=200, ev_pct=8.0) == "taker"


def test_decide_mode_high_ev_takes():
    assert decide_mode(book_depth_usd=500.0, signal_age_sec=10, ev_pct=20.0) == "taker"


def test_decide_mode_default_maker():
    assert decide_mode(book_depth_usd=500.0, signal_age_sec=10, ev_pct=8.0) == "maker"


def test_check_horizon_cap_within_30d_ok():
    end = datetime.now(UTC) + timedelta(days=15)
    ok, reason = check_horizon_cap(end)
    assert ok is True
    assert reason == ""


def test_check_horizon_cap_exactly_at_boundary_ok():
    end = datetime.now(UTC) + timedelta(days=MAX_HORIZON_DAYS) - timedelta(seconds=10)
    ok, _ = check_horizon_cap(end)
    assert ok is True


def test_check_horizon_cap_over_30d_rejected():
    end = datetime.now(UTC) + timedelta(days=MAX_HORIZON_DAYS + 5)
    ok, reason = check_horizon_cap(end)
    assert ok is False
    assert "horizon_exceeds" in reason


def test_check_horizon_cap_none_rejected():
    ok, reason = check_horizon_cap(None)
    assert ok is False
    assert reason == "missing_market_end_date"


def test_check_horizon_cap_naive_datetime_treated_as_utc():
    naive_end = datetime.utcnow() + timedelta(days=MAX_HORIZON_DAYS + 5)
    ok, reason = check_horizon_cap(naive_end)
    assert ok is False
    assert "horizon_exceeds" in reason


# ─── Mode dispatch (shadow / virtual / live) ───


def _ctx(**overrides):
    base = dict(
        detector="D7b_no_favorite_fade",
        detector_state=None,
        market_id="0xmarket1",
        token_id="tok1",
        side="NO",
        signal_price=0.10,
        stake_usd=10.0,
        bankroll_usd=1000.0,
        generated_at=datetime.now(UTC) - timedelta(seconds=10000),  # стало бы stale
        market_end_date=datetime.now(UTC) + timedelta(days=200),  # > 30d
    )
    base.update(overrides)
    return SafetyContext(**base)


@pytest.mark.asyncio
async def test_evaluate_shadow_bypasses_all_checks():
    """Shadow: paper trade — все checks bypass."""
    ctx = _ctx()
    verdict = await evaluate(ctx, mode="shadow")
    assert verdict.allowed is True
    assert verdict.reason == ""


@pytest.mark.asyncio
async def test_evaluate_virtual_bypasses_all_checks():
    """Virtual: paper trade — kill_switch / stale_signal / horizon bypass."""
    ctx = _ctx()
    verdict = await evaluate(ctx, mode="virtual")
    assert verdict.allowed is True
    assert verdict.reason == ""


@pytest.mark.asyncio
async def test_evaluate_live_runs_full_safety():
    """Live: с stale signal + horizon > 30d → reject."""
    ctx = _ctx()
    verdict = await evaluate(ctx, mode="live")
    assert verdict.allowed is False
    # Любая live check должна сработать (horizon, stale, kill_switch)
    assert verdict.reason != ""


# ─── check_bucket_disabled + shadow-телеметрия (Task 4, event-driven price pipeline) ───


@pytest.mark.asyncio
async def test_bucket_check_verdict_unchanged_when_telemetry_fails():
    mock_redis = AsyncMock()
    mock_redis.sismember = AsyncMock(return_value=False)
    with (
        patch("app.core.cache.get_redis", new_callable=AsyncMock, return_value=mock_redis),
        patch(
            "app.services.execution_safety.fetchrow",
            new_callable=AsyncMock,
            return_value={"category": "Politics", "current_price": 0.5, "yes_token_id": "t1"},
        ),
        patch(
            "app.services.execution_safety.log_price_staleness",
            new_callable=AsyncMock,
            side_effect=RuntimeError("telemetry down"),
        ),
    ):
        ok, reason = await check_bucket_disabled("D5_long_horizon_extreme_fade", "mkt1")
    assert ok is True
    assert reason == ""


@pytest.mark.asyncio
async def test_bucket_check_calls_telemetry_with_row_data():
    """Happy path: log_price_staleness вызывается с point/market_id/yes_token_id/price из row."""
    mock_redis = AsyncMock()
    mock_redis.sismember = AsyncMock(return_value=False)
    mock_telemetry = AsyncMock()
    with (
        patch("app.core.cache.get_redis", new_callable=AsyncMock, return_value=mock_redis),
        patch(
            "app.services.execution_safety.fetchrow",
            new_callable=AsyncMock,
            return_value={"category": "Politics", "current_price": 0.5, "yes_token_id": "tok_yes"},
        ),
        patch("app.services.execution_safety.log_price_staleness", mock_telemetry),
    ):
        ok, reason = await check_bucket_disabled("D5_long_horizon_extreme_fade", "mkt1")
        # fire-and-forget через asyncio.create_task — дать event loop прогнать таск
        await asyncio.sleep(0)
    assert ok is True
    assert reason == ""
    mock_telemetry.assert_awaited_once_with("bucket_gate", "mkt1", "tok_yes", 0.5)


@pytest.mark.asyncio
async def test_bucket_check_blocks_when_bucket_disabled():
    """Regression: zone-computation и bucket-key логика не задеты изменениями Task 4."""
    mock_redis = AsyncMock()
    mock_redis.sismember = AsyncMock(return_value=True)
    with (
        patch("app.core.cache.get_redis", new_callable=AsyncMock, return_value=mock_redis),
        patch(
            "app.services.execution_safety.fetchrow",
            new_callable=AsyncMock,
            return_value={"category": "Politics", "current_price": 0.5, "yes_token_id": "tok_yes"},
        ),
        patch("app.services.execution_safety.log_price_staleness", new_callable=AsyncMock),
    ):
        ok, reason = await check_bucket_disabled("D5_long_horizon_extreme_fade", "mkt1")
    assert ok is False
    assert reason == "bucket_disabled:D5_long_horizon_extreme_fade:Politics:40-60"


@pytest.mark.asyncio
async def test_bucket_check_seeds_rest_book_for_bucket_gate():
    """bucket_gate живёт на db-цене (live_coverage=0%, см. nine-point-audit) —
    seed_book_snapshot должен вызываться так же, как в mtm_refresh.py, чтобы
    следующий читатель microstructure-кэша увидел REST-посеянную live-цену."""
    mock_redis = AsyncMock()
    mock_redis.sismember = AsyncMock(return_value=False)
    mock_seed = AsyncMock()
    with (
        patch("app.core.cache.get_redis", new_callable=AsyncMock, return_value=mock_redis),
        patch(
            "app.services.execution_safety.fetchrow",
            new_callable=AsyncMock,
            return_value={"category": "Politics", "current_price": 0.5, "yes_token_id": "tok_yes"},
        ),
        patch("app.services.execution_safety.log_price_staleness", new_callable=AsyncMock),
        patch("app.services.execution_safety.seed_book_snapshot", mock_seed),
    ):
        ok, reason = await check_bucket_disabled("D5_long_horizon_extreme_fade", "mkt1")
        await asyncio.sleep(0)
    assert ok is True
    mock_seed.assert_awaited_once_with("tok_yes")


@pytest.mark.asyncio
async def test_bucket_check_zone_uses_live_price():
    """Зона bucket'а считается по live-цене из WS-кэша, не по db current_price
    (Fix-1, latency-аудит 2026-07-06): db=0.5 → зона 40-60, live=0.97 →
    gte95_blocked. Телеметрия staleness при этом остаётся на db-цене —
    она меряет расхождение db vs live."""
    mock_redis = AsyncMock()
    mock_redis.sismember = AsyncMock(return_value=True)
    with (
        patch("app.core.cache.get_redis", new_callable=AsyncMock, return_value=mock_redis),
        patch(
            "app.services.execution_safety.fetchrow",
            new_callable=AsyncMock,
            return_value={"category": "Politics", "current_price": 0.5, "yes_token_id": "tok_yes"},
        ),
        patch("app.services.execution_safety.log_price_staleness", new_callable=AsyncMock) as mock_telemetry,
        patch("app.services.execution_safety.seed_book_snapshot", new_callable=AsyncMock),
        patch(
            "app.services.execution_safety.get_live_yes_price",
            new_callable=AsyncMock,
            return_value=(0.97, True),
        ) as mock_live,
    ):
        ok, reason = await check_bucket_disabled("D5_long_horizon_extreme_fade", "mkt1")
        await asyncio.sleep(0)
    assert ok is False
    assert reason == "bucket_disabled:D5_long_horizon_extreme_fade:Politics:gte95_blocked"
    mock_live.assert_awaited_once_with("tok_yes", 0.5)
    mock_telemetry.assert_awaited_once_with("bucket_gate", "mkt1", "tok_yes", 0.5)


# ─── book_depth в SafetyVerdict → decide_mode (gates-audit F1, 2026-07-05) ───
# decide_mode получал хардкод 200.0 → depth-условие (<100 → taker) было мертво.
# check_market_book уже фетчит книгу — глубина пробрасывается через verdict.


@pytest.mark.asyncio
async def test_check_market_book_returns_depth():
    book = {"bid": 0.40, "ask": 0.42, "bid_size": 90.0, "ask_size": 67.0}
    with patch("app.services.execution_safety.fetch_live_book", new_callable=AsyncMock, return_value=book):
        ok, reason, current, depth = await check_market_book("tok1", 0.41, 10.0)
    assert ok is True
    assert depth == pytest.approx(157.0)


@pytest.mark.asyncio
async def test_check_market_book_depth_none_when_book_missing():
    with patch("app.services.execution_safety.fetch_live_book", new_callable=AsyncMock, return_value=None):
        ok, reason, current, depth = await check_market_book("tok1", 0.41, 10.0)
    assert ok is False
    assert depth is None


@pytest.mark.asyncio
async def test_evaluate_live_verdict_carries_book_depth(monkeypatch):
    import app.services.execution_safety as es

    for name in ("check_horizon_cap", "check_signal_age", "check_kill_switch", "check_detector_live"):
        monkeypatch.setattr(es, name, lambda *a, **k: (True, ""))
    monkeypatch.setattr(es, "check_balance", lambda *a, **k: (True, ""))
    for name in (
        "check_redis_kill_flag",
        "check_daemon_pause",
        "check_bucket_disabled",
        "check_max_inflight_orders",
        "check_max_open_positions",
        "check_daily_loss",
        "check_daily_pnl_limit_mtm",
    ):
        monkeypatch.setattr(es, name, AsyncMock(return_value=(True, "")))
    monkeypatch.setattr(es, "check_market_book", AsyncMock(return_value=(True, "", 0.42, 157.0)))

    verdict = await es.evaluate(_ctx(), mode="live")

    assert verdict.allowed is True
    assert verdict.book_depth_usd == pytest.approx(157.0)


@pytest.mark.asyncio
async def test_evaluate_virtual_verdict_depth_is_none():
    verdict = await evaluate(_ctx(), mode="virtual")
    assert verdict.book_depth_usd is None
