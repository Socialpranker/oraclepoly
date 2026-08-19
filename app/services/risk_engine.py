# app/services/risk_engine.py
"""Risk engine: exposure limits, portfolio heat, drawdown protection."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class OpenPosition:
    market_id: str
    event_id: str | None
    category: str
    stake: float
    created_at: datetime
    resolved_at: datetime | None


@dataclass
class RiskCheckResult:
    approved_stake: float
    original_stake: float
    reductions: list[str] = field(default_factory=list)
    is_blocked: bool = False


def check_risk(
    proposed_stake: float,
    market_id: str,
    event_id: str | None,
    category: str,
    bank: float,
    peak_bank: float,
    open_positions: list[OpenPosition],
    max_event_exposure: float = 0.10,
    max_category_exposure: float = 0.30,
    max_portfolio_heat: float = 0.40,
    drawdown_threshold: float = 0.20,
    drawdown_factor: float = 0.50,
    max_per_bet: float = 0.0,
    min_stake_usd: float = 0.0,
) -> RiskCheckResult:
    """Проверить ставку через 6 уровней risk management.

    Phase 1 Stage 3 (документ §12):
      - max_per_bet=0.05 → top cap 5% bankroll per single trade.
      - min_stake_usd=25.0 → блок если итоговая ставка < $25 (нерентабельно).

    Defaults 0.0 для backward compatibility: новые limits активируются только
    когда caller передаёт значения (через Settings в _apply_risk_engine).
    """
    reductions: list[str] = []
    stake = proposed_stake

    # 0. Per-bet cap (5% of bank) — top limit, до event/cat/heat.
    if stake > 0 and max_per_bet > 0:
        per_bet_max = bank * max_per_bet
        if stake > per_bet_max:
            stake = per_bet_max
            reductions.append("per_bet_cap")

    # 1. Event exposure
    if event_id is not None and stake > 0:
        event_used = sum(p.stake for p in open_positions if p.event_id == event_id)
        event_max = bank * max_event_exposure
        available = max(0.0, event_max - event_used)
        if stake > available:
            stake = available
            reductions.append("event_cap")

    # 2. Category exposure
    if stake > 0:
        cat_used = sum(p.stake for p in open_positions if p.category == category)
        cat_max = bank * max_category_exposure
        available = max(0.0, cat_max - cat_used)
        if stake > available:
            stake = available
            reductions.append("category_cap")

    # 3. Portfolio heat
    if stake > 0:
        total_used = sum(p.stake for p in open_positions)
        heat_max = bank * max_portfolio_heat
        available = max(0.0, heat_max - total_used)
        if stake > available:
            stake = available
            reductions.append("heat_cap")

    # 4. Drawdown reduce
    if stake > 0 and peak_bank > 0:
        drawdown_pct = (peak_bank - bank) / peak_bank
        if drawdown_pct >= drawdown_threshold:
            stake = stake * drawdown_factor
            reductions.append("drawdown_reduce")

    stake = round(max(0.0, stake), 2)

    # 5. Min stake floor — final check, blocks if reduced ниже viable threshold.
    # Лучше не fill at $5 чем съесть fee на нерентабельную ставку.
    is_blocked = stake == 0.0
    if not is_blocked and min_stake_usd > 0 and stake < min_stake_usd:
        reductions.append("below_min_stake")
        stake = 0.0
        is_blocked = True

    return RiskCheckResult(
        approved_stake=stake,
        original_stake=proposed_stake,
        reductions=reductions,
        is_blocked=is_blocked,
    )
