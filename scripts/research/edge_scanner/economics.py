"""Economics — net ROI, slippage, haircut formulas.

Pure functions. Same math as scripts/research/wallet_quality_edge_search.py
and scripts/research/d14_crypto_winners_4h_canonical.py.
"""

from __future__ import annotations

from .config import (
    DEFAULT_FEE_PCT,
    HONEST_HAIRCUT,
    SLIPPAGE_PP_BY_ZONE,
)


def fee_drag(fee_pct: float = DEFAULT_FEE_PCT) -> float:
    """Round-trip fee drag (entry + exit). 1.5% × 2 = 0.03."""
    return fee_pct * 2 / 100


def slippage_pct(zone: str) -> float:
    """Per-zone slippage as decimal (e.g. 'extreme' → 0.04)."""
    return SLIPPAGE_PP_BY_ZONE.get(zone, 3.0) / 100


def compute_net_roi(
    win_rate: float,
    avg_cost: float,
    zone: str,
    fee_pct: float = DEFAULT_FEE_PCT,
) -> float:
    """Net ROI = (WR * (1 - fees) - cost - slip) / (cost + slip).

    avg_cost is per-token cost in USD (1 - yes_price for NO, yes_price for YES).
    Returns decimal (e.g. 0.139 = +13.9%).
    """
    drag = fee_drag(fee_pct)
    slip = slippage_pct(zone)
    cost_net = avg_cost + slip
    if cost_net <= 0:
        return 0.0
    return (win_rate * (1 - drag) - cost_net) / cost_net


def compute_metrics(
    df,
    side: str,
    zone: str,
    fee_pct: float = DEFAULT_FEE_PCT,
) -> dict:
    """Compute (n, wr, avg_cost, net_roi) for given side and dataframe.

    df must have columns: yes_won (0/1), yes_price_at_anchor.
    """
    n = len(df)
    if n == 0:
        return {"n": 0, "wr": 0.0, "avg_cost": 0.0, "net_roi": 0.0}

    avg_yes = float(df["yes_price_at_anchor"].mean())
    if side == "NO":
        wins = int((df["yes_won"] == 0).sum())
        cost = 1.0 - avg_yes
    else:
        wins = int((df["yes_won"] == 1).sum())
        cost = avg_yes

    wr = wins / n if n > 0 else 0.0
    net_roi = compute_net_roi(wr, cost, zone, fee_pct)
    return {"n": n, "wr": wr, "avg_cost": cost, "net_roi": net_roi}


def daily_pnl(
    n_signals: int,
    net_roi: float,
    days: int,
    avg_stake_usd: float,
    *,
    honest: bool = False,
) -> float:
    """Estimate daily PnL from canonical sample stats.

    daily_signals = n_signals / days
    daily_raw = daily_signals * avg_stake * net_roi
    daily_honest = daily_raw * HONEST_HAIRCUT
    """
    if days <= 0:
        return 0.0
    daily_signals = n_signals / days
    raw = daily_signals * avg_stake_usd * net_roi
    return raw * HONEST_HAIRCUT if honest else raw
