"""EV-aware position sizing (Phase E.4).

Заменяет Kelly-with-inflated-edge на calibrated_p × payout - (1-p) × stake
с fractional Kelly (f=0.25) для буфера на model error.

Архитектура inference path:
    detector.detect(payload) → Signal (raw confidence)
    L1.transform(confidence) → calibrated_p (Beta)
    L2.predict(features+calibrated_p) → meta_p (LGB)
    EV-sizer(meta_p, entry_price, bankroll, category) → stake | None (skip)

При cold-start (нет ml_calibrators / ml_meta_labeler в БД) — fallback на raw
detector confidence × consвервативный fractional Kelly с f=0.10 (вместо 0.25).

Реалистичный EV (2026-05-01): дополнительно вычитаем Polymarket fee + half-spread
+ 2% buffer, чтобы не открывать позиции типа NO @ 0.99 где break-even WR ≈ 99%.
См. memory/oracle_score_audit_2026-04-30.md, scripts/research/detector_pnl_with_fees.py.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.slippage import get_spread_estimate
from app.services.fee_calculator import estimate_trading_fee

# ── Конфигурация (defaults) ──
# Sizing настроен под event-driven режим (1-15 trades/day, edge 30-50% per trade).
# В HFT-style было 0.005 cap + 0.10 Kelly — нерелевантно для нашего pattern.
# См. memory/event_driven_sizing_2026-05-02.md.
FRACTIONAL_KELLY_FULL = 0.25  # когда есть calibrated p (L1 + L2 fit'ed)
FRACTIONAL_KELLY_COLD = 0.20  # cold-start: только raw detector confidence

# User 2026-05-13: "надо искать высокий edge". Поднимаем floor с 5% до 20%.
# Backtest evidence: STANDALONE_V2_LGB edge >=100pp = WR 87.5% (n=48).
# Низкий edge (10-20pp) = WR 32% — отрицательная alpha, отсекаем.
MIN_EDGE_PCT_DEFAULT = 20.0  # high-edge focus

# Stake size — если edge действительно высокий, увеличиваем bankroll exposure.
# Old: 2% per trade fixed. New: 2% baseline, до 5% при edge >= 50pp.
MAX_STAKE_PCT_DEFAULT = 0.05  # 5% от bankroll per trade (high-edge friendly)
PAYOUT = 1.0  # Polymarket: 1 share = $1 при WIN

# Hard gate на entry_price — выше этого порога break-even WR > 99% и стратегия
# математически нерентабельна. См. scripts/research/detector_pnl_with_fees.py.
ENTRY_PRICE_CEILING = 0.95

# Buffer поверх min_edge_pct — защита от model uncertainty + ghost fills.
# Применяется ТОЛЬКО когда у нас есть category (т.е. посчитаны fee+spread).
EDGE_BUFFER_PCT = 2.0


@dataclass(frozen=True)
class SizingDecision:
    """Результат sizing: stake (USDC) или skip."""

    stake_usd: float | None  # None = skip
    reason: str
    ev_pct: float  # EV в % от stake (для логов)
    p: float  # использованная probability
    entry_price: float


def _kelly_fraction(p: float, payout: float) -> float:
    """Чистая Kelly: f* = (p*b - q) / b, где b = ЧИСТАЯ прибыль на $1, q = 1-p.

    `payout` здесь — прибыль, а НЕ валовой возврат: для входа по 0.4 шара
    возвращает $1, то есть прибыль $0.6 на $0.4 ставки → b = 1/0.4 - 1 = 1.5.
    Вызывающий обязан передавать `1/entry - 1`, см. `_net_payout`.

    Фикс 2026-08-10: раньше сюда приходил валовой возврат `1/entry`, из-за чего
    f* при p≈0.9 и entry≈0.9 давал ≈0.9 (почти весь банкролл) вместо ≈0.02.
    Ставку спасал только кап MAX_STAKE_PCT_DEFAULT, то есть Kelly фактически
    не работал. См. research/favorite-buying-audit-2026-08-10.md §2.
    """
    if p <= 0 or p >= 1:
        return 0.0
    b = payout
    q = 1.0 - p
    if b <= 0:
        return 0.0
    return (p * b - q) / b


def _net_payout(entry_price: float) -> float:
    """Чистая прибыль на $1 ставки при выигрыше: 1/entry - 1.

    Единая точка истины для EV и Kelly — обе формулы оперируют прибылью, а не
    валовым возвратом. Смешение этих двух величин было корнем завышения EV
    ровно на p×100 пп (аудит 2026-08-10).
    """
    return 1.0 / entry_price - 1.0


def _cost_drag_pct(
    entry_price: float,
    category: str,
    fee_schedule: dict | None = None,
) -> tuple[float, float]:
    """Возвращает (fee_drag_pct, spread_drag_pct) — сколько % от $1 stake съедают
    Polymarket fee и half-spread (maker mode).

    Fee — formula `app/services/fee_calculator.estimate_trading_fee` на условном
    $1 stake → gross_notional = (1+payout) → fee × 2 (entry+exit для WIN, для
    LOSS — половина, но мы берём верхнюю оценку).
    Spread — half-spread by category (`app/core/slippage.get_spread_estimate`).

    fee_schedule — опциональный per-market dict из markets.fee_schedule.
    Если задан, используем точный rate, иначе fallback на категорию.
    """
    payout = 1.0 / max(entry_price, 0.01) - 1.0
    gross_notional = 1.0 * (1.0 + payout)
    fee_one_leg = estimate_trading_fee(gross_notional, entry_price, category, fee_schedule)
    # Берём 2 ноги как worst case (WIN сценарий с redemption + sell back).
    fee_drag_pct = 2.0 * fee_one_leg * 100.0  # на $1 stake → fee в долларах × 100 = %
    spread_drag_pct = get_spread_estimate(category) * 100.0  # half-spread на $1 = %
    return fee_drag_pct, spread_drag_pct


def size_from_calibrated_p(
    *,
    calibrated_p: float,
    entry_price: float,
    bankroll_usd: float,
    category: str | None = None,
    fee_schedule: dict | None = None,
    fractional_kelly: float = FRACTIONAL_KELLY_FULL,
    min_edge_pct: float = MIN_EDGE_PCT_DEFAULT,
    max_stake_pct_of_bankroll: float = MAX_STAKE_PCT_DEFAULT,
) -> SizingDecision:
    """Решает: stake или skip.

    Math:
      payout (odds) = 1/entry_price (e.g. NO @ 0.4 → 2.5x)
      EV per $1 stake = p * payout - (1-p)         [GROSS]
      net EV       = EV - fee_drag - spread_drag    [реалистичный]
      Kelly fraction f* = (p * payout - (1-p)) / payout
      stake = bankroll * fractional_kelly * f*
      cap: stake ≤ bankroll * max_stake_pct_of_bankroll

    Hard gate: entry_price ≥ ENTRY_PRICE_CEILING → reject (break-even WR > 99%).

    category: Polymarket fee category (crypto/sports/finance/...). Если None —
    fee/spread не учитываются (legacy callers, cold-start). Рекомендуется всегда
    передавать category для реалистичной оценки.
    """
    if not (0.0 < calibrated_p < 1.0):
        return SizingDecision(None, "invalid_p", 0.0, calibrated_p, entry_price)
    if not (0.001 < entry_price < 0.999):
        return SizingDecision(None, "invalid_entry_price", 0.0, calibrated_p, entry_price)
    if bankroll_usd <= 0:
        return SizingDecision(None, "no_bankroll", 0.0, calibrated_p, entry_price)

    # Hard gate: extreme prices математически нерентабельны.
    if entry_price >= ENTRY_PRICE_CEILING:
        return SizingDecision(
            None,
            f"entry_price_ceiling ({entry_price:.3f} ≥ {ENTRY_PRICE_CEILING})",
            0.0,
            calibrated_p,
            entry_price,
        )

    # payout = ЧИСТАЯ прибыль на $1 (1/entry - 1), не валовой возврат.
    # EV = p × прибыль − (1−p) × stake. При p=0.91/entry=0.895 это +1.68%,
    # а не +92.68%, как считала прежняя формула.
    payout = _net_payout(entry_price)
    ev_per_dollar = calibrated_p * payout - (1.0 - calibrated_p)
    ev_pct = ev_per_dollar * 100.0

    # Реалистичный EV: вычитаем fee+spread+buffer (если знаем category).
    if category is not None:
        fee_drag_pct, spread_drag_pct = _cost_drag_pct(entry_price, category, fee_schedule)
        net_ev_pct = ev_pct - fee_drag_pct - spread_drag_pct
        threshold = min_edge_pct + EDGE_BUFFER_PCT
        if net_ev_pct < threshold:
            return SizingDecision(
                None,
                f"ev_below_threshold_net "
                f"(net={net_ev_pct:.2f}% gross={ev_pct:.2f}% "
                f"fee={fee_drag_pct:.2f}% spread={spread_drag_pct:.2f}% "
                f"min={threshold:.2f}%)",
                net_ev_pct,
                calibrated_p,
                entry_price,
            )
    else:
        # Legacy/cold-start path: проверяем только gross EV.
        if ev_pct < min_edge_pct:
            return SizingDecision(
                None, f"ev_below_threshold ({ev_pct:.2f}% < {min_edge_pct}%)", ev_pct, calibrated_p, entry_price
            )

    f_star = _kelly_fraction(calibrated_p, payout)
    if f_star <= 0:
        return SizingDecision(None, "negative_kelly", ev_pct, calibrated_p, entry_price)

    raw_stake = bankroll_usd * fractional_kelly * f_star
    cap = bankroll_usd * max_stake_pct_of_bankroll
    stake = min(raw_stake, cap)

    return SizingDecision(round(stake, 2), "ok", ev_pct, calibrated_p, entry_price)


def size_from_raw_confidence(
    *,
    raw_confidence: float,
    entry_price: float,
    bankroll_usd: float,
    category: str | None = None,
    fee_schedule: dict | None = None,
    min_edge_pct: float = MIN_EDGE_PCT_DEFAULT,
) -> SizingDecision:
    """Cold-start sizing: используем raw confidence детектора с консервативным f=0.10.

    Применяется когда L1/L2 ещё не fit'ed (n_train < min_samples).
    """
    return size_from_calibrated_p(
        calibrated_p=raw_confidence,
        entry_price=entry_price,
        bankroll_usd=bankroll_usd,
        category=category,
        fee_schedule=fee_schedule,
        fractional_kelly=FRACTIONAL_KELLY_COLD,
        min_edge_pct=min_edge_pct,
    )
