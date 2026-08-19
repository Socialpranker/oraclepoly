"""Backtest metrics — Wilson score interval + aggregations.

Pure math, no I/O. Все функции deterministic.

Fee model (2026-05): два режима — flat или dynamic (per-signal через
fee_calculator). Default остался flat для совместимости; новый код передаёт
FeeMode.DYNAMIC через BacktestConfig.

Production fee semantics (SSoT app/jobs/v2_execution_resolve.py):
hold-to-resolve = ENTRY-side fee only. Settlement (winning redeem @ $1)
бесплатный. Early-exit (TP/SL) добавляет exit fee — это другая ситуация,
backtest hold-to-resolve её не модулирует.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable

from app.backtest.schema import CategoryStats, FeeMode, SignalOutcome, SlippageMode
from app.core.slippage import get_spread_estimate
from app.services.fee_calculator import estimate_trading_fee

# Legacy flat fee. Used когда BacktestConfig.fee_mode == FLAT.
# Систематически завышает fees в extreme zones (p × (1-p) → 0):
# D44 entry @ 0.97 Crypto по-честному ~0.21% entry-only.
# Кроме того исторически считал round-trip, но prod берёт fee только с entry.
ROUND_TRIP_FEE_PCT_FLAT = 3.0

# z-values для two-sided CI
_Z_LOOKUP: dict[float, float] = {
    0.80: 1.282,
    0.90: 1.645,
    0.95: 1.96,
    0.99: 2.576,
}


def wilson_score_interval(
    wins: int,
    total: int,
    confidence: float = 0.95,
) -> tuple[float, float]:
    """Wilson score CI для биномиальной пропорции wins/total.

    Args:
        wins: число успехов
        total: общее число испытаний
        confidence: 0.80, 0.90, 0.95, 0.99

    Returns:
        (lower, upper) bound. nobs=0 → (0.0, 0.0).

    Examples:
        >>> wilson_score_interval(5, 10, 0.95)  # Wikipedia
        (0.237, 0.763) — approximately
        >>> wilson_score_interval(0, 0)
        (0.0, 0.0)
    """
    if total == 0:
        return 0.0, 0.0
    if wins < 0 or wins > total:
        raise ValueError(f"wins={wins} must be in [0, {total}]")

    z = _Z_LOOKUP.get(confidence)
    if z is None:
        raise ValueError(f"unsupported confidence {confidence}; use {sorted(_Z_LOOKUP)}")

    p = wins / total
    z_sq = z * z
    denom = 1 + z_sq / total
    center = (p + z_sq / (2 * total)) / denom
    spread = z * math.sqrt(p * (1 - p) / total + z_sq / (4 * total**2)) / denom

    lower = max(0.0, center - spread)
    upper = min(1.0, center + spread)
    return lower, upper


def signal_pnl_pct(outcome: SignalOutcome) -> float:
    """Gross P&L per signal в процентах от stake.

    Win:  payoff = (1 / entry_price) - 1, e.g. entry=0.30 → payoff=2.33 = +233%
    Loss: -100% (потеряли весь stake).
    """
    if outcome.won:
        if outcome.entry_price <= 0:
            return 0.0
        return (1.0 / outcome.entry_price - 1.0) * 100.0
    return -100.0


def signal_fee_pct(outcome: SignalOutcome, stake_usd: float = 100.0) -> float:
    """Polymarket entry-side taker fee для конкретного signal'а, % от stake.

    Hold-to-resolve семантика (mirror app/jobs/v2_execution_resolve.py):
    - entry: taker fee на момент входа (estimate_trading_fee)
    - settlement: winning shares автоматически выкуплены по $1 USDC — БЕЗ fee

    Поэтому единственный fee charge — entry. Early-exit (TP/SL) добавляет
    exit fee, но backtest hold-to-resolve её не моделирует.

    На D44 entry @ 0.97 Crypto: 0.072 × 0.97 × 0.03 × 100 = $0.21 = 0.21%.
    Стало сильно меньше старого flat 3% потому что fee schedule decays через
    p × (1-p) в extreme zones.
    """
    if stake_usd <= 0:
        return 0.0
    entry_fee_usd = estimate_trading_fee(
        amount=stake_usd,
        price=outcome.entry_price,
        category=outcome.category or "other",
    )
    return (entry_fee_usd / stake_usd) * 100.0


def signal_slippage_pct(
    outcome: SignalOutcome,
    slippage_mode: SlippageMode,
) -> float:
    """Per-signal slippage haircut, % от stake.

    Mirror prod v2_execution_resolve.py: slippage = stake × half_spread(category).
    half_spread — ДОЛЯ stake (не цены), калибрована по live fills.

    Modes:
    - NONE: 0
    - BASELINE: 2% от stake (фиксированный consereative haircut)
    - REALISTIC: stake × get_spread_estimate(category)
    """
    if slippage_mode is SlippageMode.NONE:
        return 0.0
    if slippage_mode is SlippageMode.BASELINE:
        return 2.0  # 2% от stake
    half_spread = get_spread_estimate(outcome.category or "other")
    return half_spread * 100.0


def aggregate_roi(
    signals: Iterable[SignalOutcome],
    fee_mode: FeeMode = FeeMode.FLAT,
    slippage_mode: SlippageMode = SlippageMode.NONE,
    stake_usd: float = 100.0,
) -> tuple[float, float]:
    """Average gross + net ROI per signal в процентах.

    Net per signal = gross - fee - slippage. Fees mode-dependent (см.
    signal_fee_pct); slippage mode-dependent (см. signal_slippage_pct).

    Returns (gross_roi_pct, net_roi_pct). Empty → (0.0, 0.0).
    """
    items = list(signals)
    if not items:
        return 0.0, 0.0
    gross_pcts = [signal_pnl_pct(s) for s in items]
    gross_avg = sum(gross_pcts) / len(gross_pcts)
    if fee_mode is FeeMode.FLAT and slippage_mode is SlippageMode.NONE:
        # Backwards-compat path: single flat haircut, no per-signal computation.
        return gross_avg, gross_avg - ROUND_TRIP_FEE_PCT_FLAT

    fee_pcts = (
        [signal_fee_pct(s, stake_usd) for s in items]
        if fee_mode is FeeMode.DYNAMIC
        else [ROUND_TRIP_FEE_PCT_FLAT] * len(items)
    )
    slip_pcts = [signal_slippage_pct(s, slippage_mode) for s in items]
    net_pcts = [g - f - sl for g, f, sl in zip(gross_pcts, fee_pcts, slip_pcts, strict=True)]
    net_avg = sum(net_pcts) / len(net_pcts)
    return gross_avg, net_avg


def aggregate_by_category(
    signals: Iterable[SignalOutcome],
    confidence: float = 0.95,
    fee_mode: FeeMode = FeeMode.FLAT,
    slippage_mode: SlippageMode = SlippageMode.NONE,
    stake_usd: float = 100.0,
) -> dict[str, CategoryStats]:
    """Group SignalOutcome list by category, compute Wilson + ROI per group."""
    buckets: dict[str, list[SignalOutcome]] = defaultdict(list)
    for sig in signals:
        buckets[sig.category or "unknown"].append(sig)

    result: dict[str, CategoryStats] = {}
    for category, items in buckets.items():
        n = len(items)
        wins = sum(1 for s in items if s.won)
        win_rate = wins / n if n > 0 else 0.0
        lower, upper = wilson_score_interval(wins, n, confidence)
        avg_edge = sum(s.expected_edge_pct for s in items) / n if n > 0 else 0.0
        gross_roi, net_roi = aggregate_roi(
            items,
            fee_mode=fee_mode,
            slippage_mode=slippage_mode,
            stake_usd=stake_usd,
        )
        result[category] = CategoryStats(
            category=category,
            n_signals=n,
            n_wins=wins,
            win_rate=win_rate,
            wilson_lower_95=lower,
            wilson_upper_95=upper,
            avg_edge_pct=avg_edge,
            gross_roi_pct=gross_roi,
            net_roi_pct=net_roi,
        )
    return result


def top_losses_by_confidence(
    signals: Iterable[SignalOutcome],
    limit: int = 10,
) -> list[SignalOutcome]:
    """Top-K losing signals отсортированные по confidence (high → low).

    Используется для error analysis — где детектор был максимально уверен
    и ошибся.
    """
    losses = [s for s in signals if not s.won]
    losses.sort(key=lambda s: s.confidence, reverse=True)
    return losses[:limit]
