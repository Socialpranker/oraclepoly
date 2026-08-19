"""Single source of truth для PnL formula.

Используется обоими:
  • app/jobs/v2_execution_resolve.py:_compute_settlement_pnl — settle filled orders,
    результат пишется в execution_orders.net_pnl/pnl/trading_fee_usd/slippage_usd
  • app/api/bot.py:activity* endpoints — counterfactual для rejected orders
    (нет stored value, надо посчитать)

ПРАВИЛО: settled orders ВСЕГДА читаются из execution_orders.net_pnl (stored).
        Эта функция вызывается ТОЛЬКО для rejected (counterfactual "if taken").

Это убирает 3 копии _net_pnl() в bot.py + divergence риск с _compute_settlement_pnl.
"""

from __future__ import annotations

from app.services.fee_calculator import estimate_trading_fee


def entry_price_for_side(side: str, market_yes_price: float) -> float:
    """Return effective entry price for a directional bet.

    YES side → платим market_yes_price.
    NO side  → платим (1 - market_yes_price).
    """
    return market_yes_price if side == "YES" else 1.0 - market_yes_price


def compute_net_pnl(
    *,
    outcome: str,
    side: str,
    market_yes_price: float,
    stake_usd: float,
    category: str | None,
    fee_schedule: dict | None = None,
) -> float:
    """Net PnL = gross_pnl − fees.

    Mirror v2_execution_resolve._compute_settlement_pnl semantics для signal_price input
    (counterfactual mode когда нет fill_price).

    Args:
      outcome: 'WIN' | 'LOSS' (EXPIRED → 0)
      side: 'YES' | 'NO'
      market_yes_price: market price на момент (signal_price для counterfactual,
                        fill_price для actual settled — но settled уже считается в resolve job)
      stake_usd: amount staked
      category: market category для fee estimation
      fee_schedule: опциональный per-market dict из markets.fee_schedule
                    (Gamma feeSchedule format). Если задан, используется точный
                    rate, иначе fallback на категорийную таблицу.

    Returns:
      Net PnL в USD. Положительное для WIN, отрицательное для LOSS, 0 для EXPIRED.
    """
    if outcome not in ("WIN", "LOSS"):
        return 0.0

    entry = entry_price_for_side(side, market_yes_price)
    if entry <= 0:
        return 0.0

    cat = (category or "other").lower()
    eff = max(entry, 0.001)
    payout = 1.0 / eff - 1.0
    # Polymarket fee per docs: amount × feeRate × p×(1-p). Резолюция = выкуп
    # winning shares за $1 без торговли, fee только на entry trade.
    # Slippage НЕ вычитается: market_yes_price/fill уже отражает цену исполнения.
    # Двойной slippage-счёт убран 2026-07-16 (аудит воронки D3, mirror
    # v2_execution_resolve._compute_settlement_pnl).
    fee_entry = estimate_trading_fee(stake_usd, eff, cat, fee_schedule)

    if outcome == "WIN":
        return stake_usd * payout - fee_entry
    # LOSS — также один entry leg.
    return -(stake_usd + fee_entry)


def compute_gross_pnl(
    *,
    outcome: str,
    side: str,
    market_yes_price: float,
    stake_usd: float,
) -> float:
    """Gross PnL без fees/slippage (для UI display)."""
    if outcome not in ("WIN", "LOSS"):
        return 0.0
    entry = entry_price_for_side(side, market_yes_price)
    if entry <= 0:
        return 0.0
    if outcome == "WIN":
        return stake_usd * (1.0 / entry - 1.0)
    return -stake_usd
