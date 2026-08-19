"""TG promote callback — mandatory gate validation перед UPDATE detector_state.

Phase 0 Stage 4. Critical security fix: раньше любой admin одним тапом
shadow→virtual→live без gate checks. См.
docs/superpowers/plans/2026-05-honest-baseline/04-tg-promote-validation.md.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from app.api.telegram import (
    CallbackQuery,
    TelegramUser,
    _evaluate_and_handle_promote,
)


def _make_transaction_mock() -> tuple[object, AsyncMock]:
    """Возвращает (transaction_context_mock, conn_execute_mock).

    Patch `app.api.telegram.transaction` через `transaction_context_mock`,
    проверяй вызовы execute через `conn_execute_mock.call_args_list`.
    """
    conn_execute_mock = AsyncMock()

    class _Conn:
        execute = conn_execute_mock

    @asynccontextmanager
    async def _transaction():
        yield _Conn()

    return _transaction, conn_execute_mock


def _make_cq(user_id: int, detector: str) -> CallbackQuery:
    """Test helper: build CallbackQuery instance."""
    return CallbackQuery(
        id=f"cq_{detector}_{user_id}",
        **{"from": TelegramUser(id=user_id, is_bot=False, first_name="Test")},
        data=f"promote:{detector}",
    )


# ── Helper that fetches detector metrics ─────────────────────────────────────


@pytest.fixture
def mock_detector_state_shadow():
    """Detector в shadow с минимальным promoted_at."""
    return {
        "stage": "shadow",
        "promoted_at": None,
        "adversarial_mirror_pass": None,
    }


@pytest.fixture
def mock_detector_state_virtual():
    """Detector в virtual stage — для virtual→live transition tests."""
    return {
        "stage": "virtual",
        "promoted_at": None,
        "adversarial_mirror_pass": True,
    }


@pytest.fixture
def mock_virtual_metrics_low_n():
    """N<30 для INSUFFICIENT_DATA scenario (fetch_virtual_metrics_7d shape — fill rate)."""
    return {
        "n_placed": 12,
        "n_filled": 8,
        "n_would_have_filled": 0,
        "wins": 5,
        "losses": 3,
        "pnl_usd": 15.0,
    }


@pytest.fixture
def mock_virtual_metrics_ready():
    """Все gates passed (fetch_virtual_metrics_7d shape — fill rate)."""
    return {
        "n_placed": 50,
        "n_filled": 45,
        "n_would_have_filled": 0,
        "wins": 30,
        "losses": 15,
        "pnl_usd": 200.0,
    }


@pytest.fixture
def mock_period_low_n():
    """Signal-level N<30 кумулятивно от promoted_at → INSUFFICIENT_DATA.

    Shape fetch_virtual_period_signals() — n_terminal/wins читаются отсюда
    (evaluate_promote_readiness), НЕ из fetch_shadow_signals_7d (устаревший
    7д-путь до унификации TG=UI)."""
    return {
        "terminal_7d": 12,
        "wins_7d": 8,
    }


@pytest.fixture
def mock_period_ready():
    """Signal-level N≥30 кумулятивно от promoted_at → n_min gate pass."""
    return {
        "terminal_7d": 50,
        "wins_7d": 40,
    }


@pytest.fixture
def mock_recent_low_n():
    """Recency 30д-окно — экономика для low-N сценария (n_min всё равно блокирует первым)."""
    return {
        "sum_roi_net_7d": 15.0,
        "mean_roi_net_7d": 1.25,
        "sum_neg_sq_roi_7d": 0.0,
        "median_entry_price_7d": None,
    }


@pytest.fixture
def mock_recent_ready():
    """Recency 30д-окно: sum_roi_net>0, Sortino=inf → G1 pass."""
    return {
        "sum_roi_net_7d": 200.0,
        "mean_roi_net_7d": 4.0,
        "sum_neg_sq_roi_7d": 0.0,  # нет downside → Sortino=inf → pass
        "median_entry_price_7d": None,
    }


# ── Virtual → Live: v2 gates path ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_promote_blocked_when_n_below_30(
    mock_detector_state_virtual, mock_virtual_metrics_low_n, mock_period_low_n, mock_recent_low_n
):
    """virtual→live с signal N=12 → INSUFFICIENT_DATA → UPDATE detector_state НЕ called.

    evaluate_promote_readiness (SSoT, детект-метрики модуль) — patch на
    fetch_virtual_period_signals (n_terminal/wins кумулятивно от promoted_at)
    и fetch_recent_window_signals (recency 30д ROI/Sortino), НЕ на устаревший
    fetch_shadow_signals_7d путь."""
    cq = _make_cq(12345, "D40_kalshi_cross_platform")

    transaction_mock, db_execute_mock = _make_transaction_mock()
    fetchrow_mock = AsyncMock(return_value=mock_detector_state_virtual)
    virtual_metrics_mock = AsyncMock(return_value={"D40_kalshi_cross_platform": mock_virtual_metrics_low_n})
    period_mock = AsyncMock(return_value={"D40_kalshi_cross_platform": mock_period_low_n})
    recent_mock = AsyncMock(return_value={"D40_kalshi_cross_platform": mock_recent_low_n})

    with (
        patch("app.api.telegram.fetchrow", fetchrow_mock),
        patch("app.api.telegram.transaction", transaction_mock),
        patch("app.services.detector_metrics.fetch_virtual_metrics_7d", virtual_metrics_mock),
        patch("app.services.detector_metrics.fetch_virtual_period_signals", period_mock),
        patch("app.services.detector_metrics.fetch_recent_window_signals", recent_mock),
        patch("app.api.telegram.answer_callback_query", AsyncMock()),
        patch("app.api.telegram.send_telegram_message", AsyncMock()),
    ):
        result = await _evaluate_and_handle_promote(cq, "D40_kalshi_cross_platform", "12345")

    # Verify: decision NOT READY
    assert result["allowed"] is False
    assert result["decision"] == "INSUFFICIENT_DATA"

    # Verify: UPDATE detector_state.stage NEVER called (только audit INSERT)
    update_calls = [c for c in db_execute_mock.call_args_list if "UPDATE detector_state" in str(c)]
    assert len(update_calls) == 0, f"expected NO UPDATE, got: {update_calls}"


# ── Manual override needed → block without UPDATE ────────────────────────────


@pytest.mark.asyncio
async def test_promote_blocked_when_adversarial_not_evaluated(
    mock_virtual_metrics_ready, mock_period_ready, mock_recent_ready
):
    """virtual→live: adversarial_pass=None → MANUAL_OVERRIDE_NEEDED → no UPDATE."""
    cq = _make_cq(12345, "D40_kalshi_cross_platform")

    state = {"stage": "virtual", "promoted_at": None, "adversarial_mirror_pass": None}
    transaction_mock, db_execute_mock = _make_transaction_mock()

    with (
        patch("app.api.telegram.fetchrow", AsyncMock(return_value=state)),
        patch("app.api.telegram.transaction", transaction_mock),
        patch(
            "app.services.detector_metrics.fetch_virtual_metrics_7d",
            AsyncMock(return_value={"D40_kalshi_cross_platform": mock_virtual_metrics_ready}),
        ),
        patch(
            "app.services.detector_metrics.fetch_virtual_period_signals",
            AsyncMock(return_value={"D40_kalshi_cross_platform": mock_period_ready}),
        ),
        patch(
            "app.services.detector_metrics.fetch_recent_window_signals",
            AsyncMock(return_value={"D40_kalshi_cross_platform": mock_recent_ready}),
        ),
        patch("app.api.telegram.answer_callback_query", AsyncMock()),
        patch("app.api.telegram.send_telegram_message", AsyncMock()),
    ):
        result = await _evaluate_and_handle_promote(cq, "D40_kalshi_cross_platform", "12345")

    assert result["allowed"] is False
    assert result["decision"] == "MANUAL_OVERRIDE_NEEDED"

    update_calls = [c for c in db_execute_mock.call_args_list if "UPDATE detector_state" in str(c)]
    assert len(update_calls) == 0


# ── All gates passed → UPDATE executed ───────────────────────────────────────


@pytest.mark.asyncio
async def test_promote_proceeds_when_all_gates_pass(mock_virtual_metrics_ready, mock_period_ready, mock_recent_ready):
    """READY → UPDATE detector_state.stage executes.

    NB: need 14d virtual для days_accumulated gate."""
    from datetime import UTC, datetime, timedelta

    cq = _make_cq(12345, "D40_kalshi_cross_platform")

    # promoted_at 20 days ago → days_virtual=20 ≥ 14
    state = {
        "stage": "virtual",
        "promoted_at": datetime.now(UTC) - timedelta(days=20),
        "adversarial_mirror_pass": True,
    }
    transaction_mock, db_execute_mock = _make_transaction_mock()

    # Plain object (не AsyncMock) для settings — нужен sync атрибутный доступ
    class _MockSettings:
        execution_enabled = True
        admin_telegram_ids = [12345]
        promote_gate_min_n_terminal = 30
        promote_gate_wilson_confidence = 0.95
        promote_gate_wilson_threshold_fill = 0.50
        promote_gate_wilson_threshold_winrate = 0.50

    mock_settings = _MockSettings()

    with (
        patch("app.api.telegram.fetchrow", AsyncMock(return_value=state)),
        patch("app.api.telegram.transaction", transaction_mock),
        patch(
            "app.services.detector_metrics.fetch_virtual_metrics_7d",
            AsyncMock(return_value={"D40_kalshi_cross_platform": mock_virtual_metrics_ready}),
        ),
        patch(
            "app.services.detector_metrics.fetch_virtual_period_signals",
            AsyncMock(return_value={"D40_kalshi_cross_platform": mock_period_ready}),
        ),
        patch(
            "app.services.detector_metrics.fetch_recent_window_signals",
            AsyncMock(return_value={"D40_kalshi_cross_platform": mock_recent_ready}),
        ),
        patch("app.api.telegram.answer_callback_query", AsyncMock()),
        patch("app.api.telegram.send_telegram_message", AsyncMock()),
        patch("app.api.telegram.get_settings", lambda: mock_settings),
        patch("app.core.config.get_settings", lambda: mock_settings),
    ):
        result = await _evaluate_and_handle_promote(cq, "D40_kalshi_cross_platform", "12345")

    assert result["allowed"] is True, f"expected READY, got: {result}"
    assert result["decision"] == "READY"

    # Verify: UPDATE detector_state.stage WAS called
    update_calls = [c for c in db_execute_mock.call_args_list if "UPDATE detector_state" in str(c)]
    assert len(update_calls) == 1


# ── Audit always written ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_audit_log_records_blocked_attempt(mock_virtual_metrics_low_n, mock_period_low_n, mock_recent_low_n):
    """Любая попытка promote (allowed или blocked) → INSERT INTO promote_attempts."""
    cq = _make_cq(12345, "D40_kalshi_cross_platform")

    state = {"stage": "virtual", "promoted_at": None, "adversarial_mirror_pass": True}
    transaction_mock, db_execute_mock = _make_transaction_mock()

    with (
        patch("app.api.telegram.fetchrow", AsyncMock(return_value=state)),
        patch("app.api.telegram.transaction", transaction_mock),
        patch(
            "app.services.detector_metrics.fetch_virtual_metrics_7d",
            AsyncMock(return_value={"D40_kalshi_cross_platform": mock_virtual_metrics_low_n}),
        ),
        patch(
            "app.services.detector_metrics.fetch_virtual_period_signals",
            AsyncMock(return_value={"D40_kalshi_cross_platform": mock_period_low_n}),
        ),
        patch(
            "app.services.detector_metrics.fetch_recent_window_signals",
            AsyncMock(return_value={"D40_kalshi_cross_platform": mock_recent_low_n}),
        ),
        patch("app.api.telegram.answer_callback_query", AsyncMock()),
        patch("app.api.telegram.send_telegram_message", AsyncMock()),
    ):
        await _evaluate_and_handle_promote(cq, "D40_kalshi_cross_platform", "12345")

    insert_calls = [c for c in db_execute_mock.call_args_list if "INSERT INTO promote_attempts" in str(c)]
    assert len(insert_calls) == 1, f"expected 1 audit INSERT, got: {insert_calls}"


# ── Detector not in any stage → error message ────────────────────────────────


@pytest.mark.asyncio
async def test_detector_already_live_returns_warning():
    """Detector в stage='live' уже на верхней ступени — нечего promote."""
    cq = _make_cq(12345, "D40_kalshi_cross_platform")
    state = {"stage": "live", "promoted_at": None, "adversarial_mirror_pass": True}

    transaction_mock, db_execute_mock = _make_transaction_mock()
    answer_mock = AsyncMock()

    with (
        patch("app.api.telegram.fetchrow", AsyncMock(return_value=state)),
        patch("app.api.telegram.transaction", transaction_mock),
        patch("app.api.telegram.fetch_virtual_metrics_7d", AsyncMock(return_value={})),
        patch("app.api.telegram.answer_callback_query", answer_mock),
        patch("app.api.telegram.send_telegram_message", AsyncMock()),
    ):
        result = await _evaluate_and_handle_promote(cq, "D40_kalshi_cross_platform", "12345")

    assert result["allowed"] is False
    assert result["decision"] == "ALREADY_AT_TOP"
    # No DB writes когда state=live
    assert db_execute_mock.call_count == 0


# ── Missing detector_state row → error ───────────────────────────────────────


@pytest.mark.asyncio
async def test_detector_state_missing_returns_error():
    """fetchrow returns None → safe error, нет crash."""
    cq = _make_cq(12345, "Unknown_Detector")

    transaction_mock, db_execute_mock = _make_transaction_mock()
    with (
        patch("app.api.telegram.fetchrow", AsyncMock(return_value=None)),
        patch("app.api.telegram.transaction", transaction_mock),
        patch("app.api.telegram.fetch_virtual_metrics_7d", AsyncMock(return_value={})),
        patch("app.api.telegram.answer_callback_query", AsyncMock()),
        patch("app.api.telegram.send_telegram_message", AsyncMock()),
    ):
        result = await _evaluate_and_handle_promote(cq, "Unknown_Detector", "12345")

    assert result["allowed"] is False
    assert result["decision"] == "DETECTOR_NOT_FOUND"
    assert db_execute_mock.call_count == 0


# ── Shadow → Virtual: honest gates v3 (≥4/5 ready) ─────────────────────────


@pytest.mark.asyncio
async def test_shadow_promote_uses_honest_gates_v3():
    """shadow→virtual использует gates v3 (sample 40 + net_pnl + tail 3% + BE_WR
    payout-aware + Sortino > 1.0).

    Все 5 gates passed: terminal 50 ≥ 40, sum_roi positive +5%, mean +1%, нет
    catastrophic, WR 66.7% > BE 33% (payout 3× при entry 0.33), Sortino > 1.0."""
    cq = _make_cq(12345, "D40_kalshi_cross_platform")
    state = {"stage": "shadow", "promoted_at": None, "adversarial_mirror_pass": None}

    transaction_mock, db_execute_mock = _make_transaction_mock()
    fetch_virtual_mock = AsyncMock(return_value={})  # должен НЕ вызываться
    fingerprint_mock = AsyncMock(return_value={})  # shadow-only → no fingerprint
    shadow_signals_mock = AsyncMock(
        return_value={
            "D40_kalshi_cross_platform": {
                "distinct_events_7d": 50,
                "terminal_7d": 50,  # ≥ 40 ✓
                "wins_7d": 35,  # WR 70% > BE 33%
                "total_7d": 60,
                "resolved_7d": 55,
                "distinct_markets_7d": 45,
                "sum_roi_net_7d": 5.0,  # +500% net ✓
                "mean_roi_net_7d": 0.10,  # +10% per signal mean
                "sum_neg_sq_roi_7d": 0.5,  # downside variance — sortino = 0.1 / √(0.5/50) = 1.0
                "median_entry_price_7d": 0.33,  # payout 3× → BE WR 25%
                "catastrophic_losses_7d": 0,  # 0% < 3% ✓
            }
        }
    )

    def backtest_map_mock():
        return {
            "D40_kalshi_cross_platform": {
                "verdict": "PASS",
                "wr_pct": 65.0,
                "honest_haircut_factor": 0.7,
                "honest_daily_usd_at_5k": 12.0,
            }
        }

    with (
        patch("app.api.telegram.fetchrow", AsyncMock(return_value=state)),
        patch("app.api.telegram.transaction", transaction_mock),
        patch("app.api.telegram.fetch_virtual_metrics_7d", fetch_virtual_mock),
        patch("app.api.telegram.fetch_shadow_signals_7d", shadow_signals_mock),
        patch("app.api.telegram.fetch_backtest_map", backtest_map_mock),
        patch("app.services.detector_metrics.fetch_virtual_fingerprints", fingerprint_mock),
        patch("app.api.telegram.answer_callback_query", AsyncMock()),
        patch("app.api.telegram.send_telegram_message", AsyncMock()),
    ):
        result = await _evaluate_and_handle_promote(cq, "D40_kalshi_cross_platform", "12345")

    # ≥4/5 gates v3 passed → READY → UPDATE detector_state CALLED
    assert result["allowed"] is True, f"expected READY, got: {result}"
    assert result["decision"] == "READY"
    update_calls = [c for c in db_execute_mock.call_args_list if "UPDATE detector_state" in str(c)]
    assert len(update_calls) == 1

    # virtual_metrics_7d НЕ должен вызываться для shadow→virtual
    fetch_virtual_mock.assert_not_called()


@pytest.mark.asyncio
async def test_shadow_promote_blocked_small_sample():
    """shadow→virtual: <40 terminal → G1 sample fail (v3 threshold 30→40)."""
    cq = _make_cq(12345, "D40_kalshi_cross_platform")
    state = {"stage": "shadow", "promoted_at": None, "adversarial_mirror_pass": None}

    transaction_mock, db_execute_mock = _make_transaction_mock()
    fingerprint_mock = AsyncMock(return_value={})
    shadow_signals_mock = AsyncMock(
        return_value={
            "D40_kalshi_cross_platform": {
                "distinct_events_7d": 10,
                "terminal_7d": 5,  # < 40
                "wins_7d": 3,
                "total_7d": 8,
                "resolved_7d": 6,
                "distinct_markets_7d": 8,
                "sum_roi_net_7d": 0.5,
                "mean_roi_net_7d": 0.10,
                "sum_neg_sq_roi_7d": 0.2,
                "median_entry_price_7d": 0.5,
                "catastrophic_losses_7d": 0,
            }
        }
    )

    def backtest_map_mock():
        return {
            "D40_kalshi_cross_platform": {
                "verdict": "PASS",
                "wr_pct": 65.0,
                "honest_haircut_factor": 0.7,
                "honest_daily_usd_at_5k": 12.0,
            }
        }

    with (
        patch("app.api.telegram.fetchrow", AsyncMock(return_value=state)),
        patch("app.api.telegram.transaction", transaction_mock),
        patch("app.api.telegram.fetch_virtual_metrics_7d", AsyncMock(return_value={})),
        patch("app.api.telegram.fetch_shadow_signals_7d", shadow_signals_mock),
        patch("app.api.telegram.fetch_backtest_map", backtest_map_mock),
        patch("app.services.detector_metrics.fetch_virtual_fingerprints", fingerprint_mock),
        patch("app.api.telegram.answer_callback_query", AsyncMock()),
        patch("app.api.telegram.send_telegram_message", AsyncMock()),
    ):
        result = await _evaluate_and_handle_promote(cq, "D40_kalshi_cross_platform", "12345")

    assert result["allowed"] is False
    assert result["decision"] == "BLOCKED"
    update_calls = [c for c in db_execute_mock.call_args_list if "UPDATE detector_state" in str(c)]
    assert len(update_calls) == 0


@pytest.mark.asyncio
async def test_shadow_promote_blocked_negative_net_pnl():
    """shadow→virtual v3: negative PnL + tail risk → ≤3/5 gates → BLOCKED.

    G1 ✅ (sample 50≥40), G2 ⛔ (PnL <0), G3 ⛔ (10% catastrophic > 3%),
    G4 ⛔ (WR 30% < BE 50% mid-zone payout), G5 ⛔ (negative Sortino).
    1/5 → BLOCKED.
    """
    cq = _make_cq(12345, "Underperformer_Detector")
    state = {"stage": "shadow", "promoted_at": None, "adversarial_mirror_pass": None}

    transaction_mock, db_execute_mock = _make_transaction_mock()
    fingerprint_mock = AsyncMock(return_value={})
    shadow_signals_mock = AsyncMock(
        return_value={
            "Underperformer_Detector": {
                "distinct_events_7d": 50,
                "terminal_7d": 50,  # G1 ✅ (≥40)
                "wins_7d": 15,  # WR 30%
                "total_7d": 60,
                "resolved_7d": 55,
                "distinct_markets_7d": 45,
                "sum_roi_net_7d": -2.0,  # G2 ⛔
                "mean_roi_net_7d": -0.04,  # negative mean → Sortino ⛔
                "sum_neg_sq_roi_7d": 5.0,  # большой downside
                "median_entry_price_7d": 0.5,  # mid-zone payout 2× → BE WR 33%
                "catastrophic_losses_7d": 5,  # 10% > 3% → G3 ⛔
            }
        }
    )

    def backtest_map_mock():
        return {}

    with (
        patch("app.api.telegram.fetchrow", AsyncMock(return_value=state)),
        patch("app.api.telegram.transaction", transaction_mock),
        patch("app.api.telegram.fetch_virtual_metrics_7d", AsyncMock(return_value={})),
        patch("app.api.telegram.fetch_shadow_signals_7d", shadow_signals_mock),
        patch("app.api.telegram.fetch_backtest_map", backtest_map_mock),
        patch("app.services.detector_metrics.fetch_virtual_fingerprints", fingerprint_mock),
        patch("app.api.telegram.answer_callback_query", AsyncMock()),
        patch("app.api.telegram.send_telegram_message", AsyncMock()),
    ):
        result = await _evaluate_and_handle_promote(cq, "Underperformer_Detector", "12345")

    assert result["allowed"] is False
    assert result["decision"] == "BLOCKED"
