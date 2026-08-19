"""Тесты detector_signals_resolve job (side-aware ROI)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.core.slippage import get_spread_estimate
from app.jobs.detector_signals_resolve import (
    _entry_price_for_side,
    _proxy_winner,
    _realized_roi_net_pct,
    _realized_roi_pct,
    _signal_outcome,
    run_detector_signals_resolve,
)
from app.services.pnl_formulas import compute_net_pnl


def test_signal_outcome_yes_winner():
    assert _signal_outcome("YES", "yes") == "WIN"
    assert _signal_outcome("NO", "yes") == "LOSS"


def test_signal_outcome_no_winner():
    assert _signal_outcome("NO", "no") == "WIN"
    assert _signal_outcome("YES", "no") == "LOSS"


def test_signal_outcome_void():
    assert _signal_outcome("YES", None) is None
    assert _signal_outcome("NO", "void") is None
    assert _signal_outcome("YES", "") is None


# ─── _proxy_winner (фикс №2: proxy-resolve EXPIRED по ценовому экстремуму) ────


def test_proxy_winner_extreme_high_is_yes():
    """Цена YES ≥ 0.98 → исход фактически предрешён в пользу YES."""
    assert _proxy_winner(0.98) == "yes"
    assert _proxy_winner(0.999) == "yes"
    assert _proxy_winner(1.0) == "yes"


def test_proxy_winner_extreme_low_is_no():
    """Цена YES ≤ 0.02 → исход фактически предрешён в пользу NO."""
    assert _proxy_winner(0.02) == "no"
    assert _proxy_winner(0.001) == "no"
    assert _proxy_winner(0.0) == "no"


def test_proxy_winner_mid_zone_is_unknown():
    """Mid-zone — честно unknown, ждём UMA (классификация evidence/07)."""
    assert _proxy_winner(0.03) is None
    assert _proxy_winner(0.45) is None
    assert _proxy_winner(0.97) is None


def test_proxy_winner_none_price_is_unknown():
    """markets.current_price отсутствует (рынок удалён/NULL) → unknown."""
    assert _proxy_winner(None) is None


# ─── _entry_price_for_side ────────────────────────────────────────────────────


def test_entry_price_yes_is_current():
    assert _entry_price_for_side(0.40, "YES") == 0.40
    assert _entry_price_for_side(0.10, "YES") == 0.10


def test_entry_price_no_is_complement():
    """current_price хранится как YES-цена; для NO платим (1 - YES)."""
    assert _entry_price_for_side(0.40, "NO") == pytest.approx(0.60)
    assert _entry_price_for_side(0.10, "NO") == pytest.approx(0.90)
    assert _entry_price_for_side(0.95, "NO") == pytest.approx(0.05)


# ─── _realized_roi_pct (side-aware) ───────────────────────────────────────────


def test_realized_roi_yes_win_at_low_price():
    """YES @ 0.40 WIN: payout = 1/0.40 - 1 = 1.5 → ROI = 150%."""
    assert _realized_roi_pct("WIN", 0.40, "YES") == 150.0


def test_realized_roi_yes_win_at_extreme_price():
    """YES @ 0.05 WIN: payout = 1/0.05 - 1 = 19 → ROI = 1900%."""
    assert _realized_roi_pct("WIN", 0.05, "YES") == 1900.0


def test_realized_roi_no_win_at_low_yes_price():
    """NO @ current_price=0.10 (entry NO=0.90) WIN: payout = 1/0.90-1 = 0.111 → ROI ≈ 11.1%.

    Это критичный case: до фикса формула YES-style давала 900% (баг).
    """
    roi = _realized_roi_pct("WIN", 0.10, "NO")
    assert roi == pytest.approx(11.111, abs=0.01)


def test_realized_roi_no_win_at_balanced_price():
    """NO @ current_price=0.40 (entry NO=0.60) WIN: payout = 1/0.60-1 = 0.667 → ROI ≈ 66.7%."""
    roi = _realized_roi_pct("WIN", 0.40, "NO")
    assert roi == pytest.approx(66.667, abs=0.01)


def test_realized_roi_no_win_at_high_yes_price():
    """NO @ current_price=0.80 (entry NO=0.20) WIN: payout = 1/0.20-1 = 4 → ROI = 400%."""
    assert _realized_roi_pct("WIN", 0.80, "NO") == 400.0


def test_realized_roi_loss_always_minus_100():
    assert _realized_roi_pct("LOSS", 0.40, "YES") == -100.0
    assert _realized_roi_pct("LOSS", 0.40, "NO") == -100.0


def test_realized_roi_zero_or_extreme_price_safe():
    assert _realized_roi_pct("WIN", 0.0, "YES") == 0.0
    assert _realized_roi_pct("WIN", 1.0, "NO") == 0.0


def test_realized_roi_clipped_to_db_precision_cap():
    """Регрессия: D43_resolution_dispute YES @ 0.0005 даёт math ROI=199,900%,
    что > numeric(8,3) max 99999.999 и роняет UPDATE batch (asyncpg
    NumericValueOutOfRangeError). Должны clip к 99999.0 (incident 2026-05-19).
    """
    roi = _realized_roi_pct("WIN", 0.0005, "YES")
    assert roi == 99_999.0  # clipped к ROI_PCT_CAP, не 199900.0
    # NO side: current_price=0.9995 → entry NO=0.0005 → тот же overflow
    roi_no = _realized_roi_pct("WIN", 0.9995, "NO")
    assert roi_no == 99_999.0


def test_realized_roi_net_clipped_to_db_precision_cap():
    """Аналогичный clip для net ROI (numeric(8,3))."""
    net = _realized_roi_net_pct("WIN", 0.0005, "YES", "crypto")
    assert -99_999.0 <= net <= 99_999.0
    net_no = _realized_roi_net_pct("WIN", 0.9995, "NO", "crypto")
    assert -99_999.0 <= net_no <= 99_999.0


# ─── _realized_roi_net_pct (side-aware с fee + slippage) ──────────────────────


def test_realized_roi_net_yes_win_low_entry_high_payout():
    """YES @ 0.40 WIN per-notional: net_pnl/(stake/cost)×100. notional=2.5,
    payout=2.5; net_pnl≈1.38 → ROI≈55%. Per-notional всегда ниже per-stake gross."""
    net = _realized_roi_net_pct("WIN", 0.40, "YES", "crypto")
    assert 30.0 < net < 70.0


def test_realized_roi_net_no_win_at_low_yes_price_negative():
    """NO @ YES=0.10 (cost NO=0.90) WIN per-notional: notional=1/0.9≈1.11.
    Низкий payout (1.11) и fee+slip быстро съедают edge.
    """
    net = _realized_roi_net_pct("WIN", 0.10, "NO", "crypto")
    # per-notional gross ≈ 10%, net меньше
    assert net < 10.0


def test_realized_roi_net_loss_includes_fee():
    """LOSS per-notional: -(stake+fee_one_leg+slip)/notional×100.
    Cost NO=0.60, notional≈1.67, потеря ≈ 1.0+fees → ROI ≈ -62%.
    Net должен быть ниже -50% но выше -100% (per-notional).
    """
    net = _realized_roi_net_pct("LOSS", 0.40, "NO", "crypto")
    assert -100.0 < net < -50.0


def test_realized_roi_net_void_safe():
    """outcome != WIN/LOSS → 0."""
    assert _realized_roi_net_pct("EXPIRED", 0.40, "NO", "crypto") == 0.0
    assert _realized_roi_net_pct("VOID", 0.40, "YES", "crypto") == 0.0


def test_realized_roi_net_zero_price_safe():
    # Регрессия half-spread fix: вырожденность проверяется по МИДУ. Иначе
    # entry=0+half_spread даёт ROI≈+97% — сломанный вход как «почти бесплатный
    # выигрыш». Проверка по цене со спредом этот случай пропускала.
    assert _realized_roi_net_pct("WIN", 0.0, "YES", "crypto") == 0.0
    assert _realized_roi_net_pct("WIN", 1.0, "NO", "crypto") == 0.0


def test_realized_roi_net_no_category_fallback():
    """category=None → используется 'other' fallback, не падаем."""
    net = _realized_roi_net_pct("WIN", 0.40, "YES", None)
    assert net != 0.0


# ─── Fee-convention parity с settlement SSoT (fix 2026-06-04) ─────────────────


def test_realized_roi_net_matches_settlement_ssot_win():
    """REGRESSION (fee-convention fix 2026-06-04): realized_roi_net должен
    деривиться из canonical compute_net_pnl (1× fee на stake), а НЕ из старой
    формулы (2×fee на gross_notional), которую код сам зовёт «inflated-fee bug».

    WIN YES@0.40 crypto: точное значение 59.309%.
    Slippage double-count fix 2026-07-16: раньше compute_net_pnl вычитал ещё и
    stake×half_spread поверх fee (double-count с уже учтённым в fill_price/
    market_yes_price slippage) — давало 58.109%. Убрали двойной вычет →
    59.309%. Этот тест провалится, если кто-то вернёт старую 2×fee формулу
    (55.344%) или вернёт двойной slippage-вычет (58.109%).
    """
    net = _realized_roi_net_pct("WIN", 0.40, "YES", "crypto")
    # Half-spread fix 2026-08-05: вход считается по ask (0.40+0.025), не по миду.
    assert net == pytest.approx(56.241, abs=0.01)
    # Явно НЕ старые забагованные значения:
    assert net != pytest.approx(55.344, abs=0.01)  # 2×fee bug
    assert net != pytest.approx(58.109, abs=0.01)  # double-counted slippage
    assert net != pytest.approx(59.309, abs=0.01)  # mid вместо ask (до 2026-08-05)

    # Parity со SSoT — по цене СО СПРЕДОМ: notional = 1/0.425.
    hs = get_spread_estimate("crypto")
    fill = 0.40 + hs
    net_pnl = compute_net_pnl(outcome="WIN", side="YES", market_yes_price=fill, stake_usd=1.0, category="crypto")
    assert net == pytest.approx(net_pnl / (1.0 / fill) * 100.0, abs=0.001)

    # Спред учтён РОВНО ОДИН раз: двойной вычет дал бы заметно меньше.
    double = (
        compute_net_pnl(outcome="WIN", side="YES", market_yes_price=0.40 + 2 * hs, stake_usd=1.0, category="crypto")
        / (1.0 / 0.45)
        * 100.0
    )
    assert net > double + 1.0, "спред вычтен дважды"


def test_realized_roi_net_matches_settlement_ssot_loss():
    """LOSS тоже должен использовать 1 ногу fee на stake (hold-to-resolution =
    один entry-leg). LOSS NO@(YES=0.40) crypto = -61.037%, и равно
    compute_net_pnl/notional×100 (notional NO = 1/0.60).

    Slippage double-count fix 2026-07-16: раньше -62.837% (двойной вычет
    stake×half_spread). Убрали двойной вычет → -61.037%.
    """
    net = _realized_roi_net_pct("LOSS", 0.40, "NO", "crypto")
    # Half-spread fix: NO-вход = (1-0.40)+0.025 = 0.625.
    assert net == pytest.approx(-64.057, abs=0.01)
    assert net != pytest.approx(-61.037, abs=0.01)  # mid вместо ask (до 2026-08-05)
    fill_no = 0.60 + get_spread_estimate("crypto")
    net_pnl = compute_net_pnl(
        outcome="LOSS", side="NO", market_yes_price=1.0 - fill_no, stake_usd=1.0, category="crypto"
    )
    assert net == pytest.approx(net_pnl / (1.0 / fill_no) * 100.0, abs=0.001)


def test_realized_roi_net_win_loss_fee_symmetry():
    """WIN и LOSS на симметричных entry должны использовать ОДИНАКОВОЕ число ног
    fee (1). Старый баг брал 2 ноги на WIN, 1 на LOSS → асимметрия, бьющая по
    high-WR детекторам. Проверяем что fee-компонент идентичен через gross−net.
    """

    for price in (0.30, 0.40, 0.55, 0.70):
        for side in ("YES", "NO"):
            # fee «съедание» на одной ноге. Сравниваем с canonical formula
            # (1-ногая для обоих исходов), считая её по цене СО СПРЕДОМ —
            # half-spread fix 2026-08-05.
            net_win = _realized_roi_net_pct("WIN", price, side, "crypto")

            entry = (price if side == "YES" else 1.0 - price) + get_spread_estimate("crypto")
            notional = 1.0 / max(entry, 0.001)
            # YES-эквивалент нашего fill: для NO вход (1-yes)+hs → yes = 1-fill.
            fill_yes = entry if side == "YES" else 1.0 - entry

            ssot_win = compute_net_pnl(
                outcome="WIN", side=side, market_yes_price=fill_yes, stake_usd=1.0, category="crypto"
            )
            assert net_win == pytest.approx(ssot_win / notional * 100.0, abs=0.01), (
                f"WIN {side}@{price}: net={net_win} != ssot-derived"
            )


def test_realized_roi_net_per_notional_smaller_magnitude_than_gross():
    """Per-notional net меньше по абсолюту чем per-stake gross на коэф. notional.

    _realized_roi_pct — per-stake (legacy для ML target), _realized_roi_net_pct —
    per-notional. notional = stake/cost > 1, поэтому |net per-notional| < |gross
    per-stake| при том же P&L. Single sanity check вместо точного сравнения.
    """
    for price in (0.20, 0.40, 0.60, 0.80, 0.94):
        for outcome in ("WIN", "LOSS"):
            for side in ("YES", "NO"):
                gross = _realized_roi_pct(outcome, price, side)
                net = _realized_roi_net_pct(outcome, price, side, "crypto")
                # per-notional всегда ближе к нулю по абсолюту
                assert abs(net) <= abs(gross) + 0.01, (
                    f"|net|={abs(net)} > |gross|={abs(gross)} at price={price}, outcome={outcome}, side={side}"
                )


# ─── run_detector_signals_resolve (integration) ───────────────────────────────


def _row(
    side,
    market_resolved,
    winner,
    current_price,
    generated_at=None,
    horizon_sec=1800,
    market_price_now=None,
):
    return {
        "id": uuid4(),
        "detector": "D1_fade_whale",
        "market_id": "mkt-1",
        "side": side,
        "current_price": current_price,
        "horizon_sec": horizon_sec,
        "generated_at": generated_at or datetime.now(UTC),
        "market_resolved": market_resolved,
        "market_winner": winner,
        "category": "crypto",
        "market_price_now": market_price_now,
    }


def _patch_fetch(main_rows, reconcile_rows=None):
    """fetch вызывается дважды: main resolve + reconcile proxy-строк."""
    return patch(
        "app.jobs.detector_signals_resolve.fetch",
        new_callable=AsyncMock,
        side_effect=[main_rows, reconcile_rows or []],
    )


@pytest.mark.asyncio
async def test_resolve_no_signals():
    with _patch_fetch([]):
        result = await run_detector_signals_resolve()
    assert result == {
        "checked": 0,
        "wins": 0,
        "losses": 0,
        "expired": 0,
        "proxy_wins": 0,
        "proxy_losses": 0,
        "proxy_reconciled": 0,
        "proxy_flipped": 0,
    }


@pytest.mark.asyncio
async def test_resolve_market_winner_yes_signal_no():
    """NO-сигнал, market resolved YES → LOSS."""
    rows = [_row(side="NO", market_resolved=True, winner="yes", current_price=0.40)]
    with (
        _patch_fetch(rows),
        patch("app.jobs.detector_signals_resolve.execute", new_callable=AsyncMock) as mock_exec,
    ):
        result = await run_detector_signals_resolve()
    assert result["losses"] == 1
    assert result["wins"] == 0
    args = mock_exec.call_args[0]
    assert args[2] == ["LOSS"]
    assert args[3] == [-100.0]
    # Подтверждённый рыночный резолв — НЕ proxy
    assert args[5] == [False]


@pytest.mark.asyncio
async def test_resolve_no_signal_wins_uses_no_entry_payout():
    """NO-сигнал @ current_price=0.40 (entry NO=0.60), market resolved NO → WIN.

    ROI = (1/0.60 - 1) * 100 = 66.667%, НЕ 150% (как было до фикса).
    """
    rows = [_row(side="NO", market_resolved=True, winner="no", current_price=0.40)]
    with (
        _patch_fetch(rows),
        patch("app.jobs.detector_signals_resolve.execute", new_callable=AsyncMock) as mock_exec,
    ):
        result = await run_detector_signals_resolve()
    assert result["wins"] == 1
    args = mock_exec.call_args[0]
    assert args[2] == ["WIN"]
    assert args[3][0] == pytest.approx(66.667, abs=0.01)


@pytest.mark.asyncio
async def test_resolve_yes_signal_wins_uses_yes_entry_payout():
    """YES-сигнал @ current_price=0.40 (entry YES=0.40), market resolved YES → WIN.

    ROI = (1/0.40 - 1) * 100 = 150%.
    """
    rows = [_row(side="YES", market_resolved=True, winner="yes", current_price=0.40)]
    with (
        _patch_fetch(rows),
        patch("app.jobs.detector_signals_resolve.execute", new_callable=AsyncMock) as mock_exec,
    ):
        result = await run_detector_signals_resolve()
    assert result["wins"] == 1
    args = mock_exec.call_args[0]
    assert args[2] == ["WIN"]
    assert args[3] == [150.0]


@pytest.mark.asyncio
async def test_resolve_expired_when_old_and_market_unresolved():
    """Сигнал старше 6 × horizon_sec, market не resolved, цена в mid-zone → EXPIRED."""
    old_ts = datetime.now(UTC) - timedelta(hours=10)  # 36000s, horizon 1800 × 6 = 10800s
    rows = [
        _row(
            side="NO",
            market_resolved=False,
            winner=None,
            current_price=0.45,
            generated_at=old_ts,
            horizon_sec=1800,
            market_price_now=0.45,
        )
    ]
    with (
        _patch_fetch(rows),
        patch("app.jobs.detector_signals_resolve.execute", new_callable=AsyncMock) as mock_exec,
    ):
        result = await run_detector_signals_resolve()
    assert result["expired"] == 1
    args = mock_exec.call_args[0]
    assert args[2] == ["EXPIRED"]
    assert args[3] == [0.0]
    assert args[5] == [False]


# ─── Proxy-resolve EXPIRED по ценовому экстремуму (фикс №2, аудит 2026-07-05) ─


@pytest.mark.asyncio
async def test_resolve_expired_at_extreme_high_yes_side_proxy_win():
    """Таймаут + markets.current_price=0.99 (YES предрешён), side=YES → proxy WIN.

    ROI считается от entry-цены сигнала (0.40), как при реальном резолве:
    (1/0.40 − 1) × 100 = 150%.
    """
    old_ts = datetime.now(UTC) - timedelta(hours=10)
    rows = [
        _row(
            side="YES",
            market_resolved=False,
            winner=None,
            current_price=0.40,
            generated_at=old_ts,
            market_price_now=0.99,
        )
    ]
    with (
        _patch_fetch(rows),
        patch("app.jobs.detector_signals_resolve.execute", new_callable=AsyncMock) as mock_exec,
    ):
        result = await run_detector_signals_resolve()
    assert result["proxy_wins"] == 1
    assert result["expired"] == 0
    assert result["wins"] == 0  # подтверждённые wins отдельно от proxy
    args = mock_exec.call_args[0]
    assert args[2] == ["WIN"]
    assert args[3] == [150.0]
    assert args[5] == [True]  # outcome_proxy


@pytest.mark.asyncio
async def test_resolve_expired_at_extreme_low_yes_side_proxy_loss():
    """Таймаут + markets.current_price=0.01 (NO предрешён), side=YES → proxy LOSS."""
    old_ts = datetime.now(UTC) - timedelta(hours=10)
    rows = [
        _row(
            side="YES",
            market_resolved=False,
            winner=None,
            current_price=0.40,
            generated_at=old_ts,
            market_price_now=0.01,
        )
    ]
    with (
        _patch_fetch(rows),
        patch("app.jobs.detector_signals_resolve.execute", new_callable=AsyncMock) as mock_exec,
    ):
        result = await run_detector_signals_resolve()
    assert result["proxy_losses"] == 1
    assert result["expired"] == 0
    args = mock_exec.call_args[0]
    assert args[2] == ["LOSS"]
    assert args[3] == [-100.0]
    assert args[5] == [True]


@pytest.mark.asyncio
async def test_resolve_expired_at_extreme_no_side_proxy_win_uses_no_entry():
    """side=NO, цена упала к 0.01 → proxy WIN с NO-entry payout.

    entry NO = 1 − 0.45 = 0.55 → ROI = (1/0.55 − 1) × 100 ≈ 81.818%.
    """
    old_ts = datetime.now(UTC) - timedelta(hours=10)
    rows = [
        _row(
            side="NO",
            market_resolved=False,
            winner=None,
            current_price=0.45,
            generated_at=old_ts,
            market_price_now=0.01,
        )
    ]
    with (
        _patch_fetch(rows),
        patch("app.jobs.detector_signals_resolve.execute", new_callable=AsyncMock) as mock_exec,
    ):
        result = await run_detector_signals_resolve()
    assert result["proxy_wins"] == 1
    args = mock_exec.call_args[0]
    assert args[2] == ["WIN"]
    assert args[3][0] == pytest.approx(81.818, abs=0.01)


@pytest.mark.asyncio
async def test_resolve_expired_null_market_price_stays_expired():
    """markets.current_price NULL (рынок удалён из markets) → честный EXPIRED."""
    old_ts = datetime.now(UTC) - timedelta(hours=10)
    rows = [
        _row(
            side="NO",
            market_resolved=False,
            winner=None,
            current_price=0.45,
            generated_at=old_ts,
            market_price_now=None,
        )
    ]
    with (
        _patch_fetch(rows),
        patch("app.jobs.detector_signals_resolve.execute", new_callable=AsyncMock) as mock_exec,
    ):
        result = await run_detector_signals_resolve()
    assert result["expired"] == 1
    args = mock_exec.call_args[0]
    assert args[2] == ["EXPIRED"]


# ─── Reconcile: proxy-строки перепроверяются при реальном резолве рынка ───────


def _proxy_row(side, winner, current_price, outcome):
    return {
        "id": uuid4(),
        "detector": "D3_cross_mkt_arb",
        "side": side,
        "current_price": current_price,
        "outcome": outcome,
        "market_winner": winner,
        "category": "crypto",
        "fee_schedule": None,
    }


@pytest.mark.asyncio
async def test_reconcile_confirms_proxy_outcome():
    """Рынок реально резолвнулся тем же исходом → флаг снят, outcome остаётся."""
    proxy_rows = [_proxy_row(side="YES", winner="yes", current_price=0.40, outcome="WIN")]
    with (
        _patch_fetch([], proxy_rows),
        patch("app.jobs.detector_signals_resolve.execute", new_callable=AsyncMock) as mock_exec,
    ):
        result = await run_detector_signals_resolve()
    assert result["proxy_reconciled"] == 1
    assert result["proxy_flipped"] == 0
    args = mock_exec.call_args[0]
    assert args[2] == ["WIN"]
    assert args[3] == [150.0]


@pytest.mark.asyncio
async def test_reconcile_flips_wrong_proxy_outcome():
    """UMA-резолв опроверг proxy (экстремум флипнулся) → outcome исправлен."""
    proxy_rows = [_proxy_row(side="YES", winner="no", current_price=0.40, outcome="WIN")]
    with (
        _patch_fetch([], proxy_rows),
        patch("app.jobs.detector_signals_resolve.execute", new_callable=AsyncMock) as mock_exec,
    ):
        result = await run_detector_signals_resolve()
    assert result["proxy_reconciled"] == 1
    assert result["proxy_flipped"] == 1
    args = mock_exec.call_args[0]
    assert args[2] == ["LOSS"]
    assert args[3] == [-100.0]


@pytest.mark.asyncio
async def test_resolve_skips_void_winner():
    """winner='void' → outcome=None → не пишем строку (skip)."""
    rows = [_row(side="YES", market_resolved=True, winner="void", current_price=0.5)]
    with (
        _patch_fetch(rows),
        patch("app.jobs.detector_signals_resolve.execute", new_callable=AsyncMock) as mock_exec,
    ):
        result = await run_detector_signals_resolve()
    assert result["wins"] == 0
    assert result["losses"] == 0
    assert result["expired"] == 0
    mock_exec.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_batch_mixed():
    """Смешанная пачка: WIN + LOSS + EXPIRED."""
    old_ts = datetime.now(UTC) - timedelta(hours=10)
    rows = [
        _row(side="NO", market_resolved=True, winner="no", current_price=0.40),  # WIN
        _row(side="YES", market_resolved=True, winner="no", current_price=0.55),  # LOSS
        _row(
            side="NO",
            market_resolved=False,
            winner=None,
            current_price=0.45,
            generated_at=old_ts,
            market_price_now=0.45,
        ),  # EXPIRED (mid-zone)
        _row(
            side="YES",
            market_resolved=False,
            winner=None,
            current_price=0.40,
            generated_at=old_ts,
            market_price_now=0.99,
        ),  # proxy WIN (экстремум)
    ]
    with (
        _patch_fetch(rows),
        patch("app.jobs.detector_signals_resolve.execute", new_callable=AsyncMock) as mock_exec,
    ):
        result = await run_detector_signals_resolve()
    assert result["wins"] == 1
    assert result["losses"] == 1
    assert result["expired"] == 1
    assert result["proxy_wins"] == 1
    args = mock_exec.call_args[0]
    assert sorted(args[2]) == ["EXPIRED", "LOSS", "WIN", "WIN"]
    assert args[5] == [False, False, False, True]


@pytest.mark.asyncio
async def test_resolve_passes_per_detector_multipliers_to_fetch():
    """fetch() получает detector_keys/values из DETECTOR_HORIZON_MULTIPLIER.

    Гарантирует что D45 short-horizon signals не EXPIRED-ятся в дефолтные
    6×horizon=30-180s, а ждут market.resolved до 600×horizon.
    """
    from app.jobs.detector_signals_resolve import (
        DETECTOR_HORIZON_MULTIPLIER,
        EXPIRE_HORIZON_MULTIPLIER,
    )

    fetch_mock = AsyncMock(return_value=[])
    with patch("app.jobs.detector_signals_resolve.fetch", fetch_mock):
        await run_detector_signals_resolve()

    # Первый fetch — main resolve (второй — reconcile proxy-строк)
    args = fetch_mock.call_args_list[0][0]
    # args[0] = SQL, args[1] = default multiplier, args[2] = batch_limit,
    # args[3] = detector_keys list, args[4] = multiplier values list
    assert args[1] == EXPIRE_HORIZON_MULTIPLIER
    assert args[3] == list(DETECTOR_HORIZON_MULTIPLIER.keys())
    assert args[4] == [DETECTOR_HORIZON_MULTIPLIER[k] for k in args[3]]
    # D45 присутствует с большим multiplier
    assert "d45_chainlink_leadlag" in args[3]
    idx = args[3].index("d45_chainlink_leadlag")
    assert args[4][idx] >= 600
    # D3 — обратный случай: короткий HORIZON_SEC (6ч), но реальные события живут
    # до 30 дней (d3_scan.py). Multiplier должен покрывать полный scan-window,
    # иначе большинство сигналов EXPIRE-ится раньше фактического резолва рынка
    # (audit 2026-07-03: 79% D3-сигналов истории были EXPIRED из-за default 6×).
    assert "D3_cross_mkt_arb" in args[3]
    idx_d3 = args[3].index("D3_cross_mkt_arb")
    assert args[4][idx_d3] >= 120
    # M2 — тот же класс, но причина в семантике HORIZON_SEC: у M2 это «сигнал живёт
    # 60м для snapshot-writer», а не время до резолва (m2_liquidity_vacuum.py:38).
    # Default 6× = 6ч при медиане времени до end_date 162.5ч → 69.8% EXPIRED с roi=0
    # (аудит 2026-08-04). Порог 4300 держит покрытие на p90 (179д = 91.4% сигналов);
    # ниже 720 (30д) покрытие падает до 64.7% и метрики снова видят меньшинство.
    assert "M2_liquidity_vacuum" in args[3]
    idx_m2 = args[3].index("M2_liquidity_vacuum")
    assert args[4][idx_m2] >= 4300
