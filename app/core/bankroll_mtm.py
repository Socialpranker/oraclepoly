"""MtM-aware bankroll расчёт. Single source of truth.

Phase 1 Stage 1. См.
docs/superpowers/plans/2026-05-risk-engine-hardening/01-mtm-daily-pnl.md.

Используется в:
- execution_safety.check_daily_pnl_limit_mtm (Stage 1.5)
- scripts/risk_daemon.py (Stage 5) — Phase 1 follow-up

Semantic invariants (унифицированы с position_manager._unrealized_pnl_pct,
SSoT app/jobs/position_manager.py:145-172):
  - entry_price хранится в координатах нашей стороны:
      YES side → entry_price = YES price at entry
      NO  side → entry_price = NO  price at entry = 1 − yes_price_at_fill
  - current_price = canonical YES price (markets.current_price)
  - YES equity per share = current_price
  - NO  equity per share = 1 − current_price
  - shares per position = stake / entry_price
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class MtMPositionSnapshot:
    """One open position для MtM расчёта.

    Args:
        order_id: identifier (для warnings и логов).
        stake: USD invested.
        entry_price: цена fill в координатах side (YES side: YES price;
                     NO side: NO price = 1 − YES_at_fill).
        current_price: текущая YES price (canonical из markets.current_price).
                       None если WS/REST не дали данные.
        side: "YES" | "NO".
    """

    order_id: str
    stake: float
    entry_price: float
    current_price: float | None
    side: str


@dataclass(frozen=True)
class BankrollMtMResult:
    """Result MtM bankroll расчёта.

    Args:
        realized_balance: wallet free USDC (deployable cash).
        unrealized_pnl: sum of per-position (equity − stake).
        mtm_equity: wallet + frozen_stake + unrealized_pnl. Это «честная» точка
                    отсчёта для daily PnL limit.
        warnings: список диагностики для skipped positions.
    """

    realized_balance: float
    unrealized_pnl: float
    mtm_equity: float
    warnings: list[str] = field(default_factory=list)


def compute_bankroll_with_mtm(
    *,
    wallet_balance: float,
    frozen_stake: float,
    open_positions: list[MtMPositionSnapshot],
) -> BankrollMtMResult:
    """MtM-aware bankroll snapshot.

    Args:
        wallet_balance: free USDC в кошельке (fetched через wallet_resolver).
        frozen_stake: sum(stake) для positions in placed/filled (locked).
        open_positions: list позиций для MtM. Если пустой — unrealized=0.

    Returns:
        BankrollMtMResult.

    Conservative behaviors:
      - current_price=None → unrealized=0 для этой позиции + warning.
      - entry_price <= 0 или >= 1 → skip + warning (повреждённые данные).
      - side ∉ {YES, NO} → skip + warning.
      - NO equity per share clipped to >= 0 (extreme YES = 0.999 → NO = 0.001).
    """
    warnings: list[str] = []
    unrealized = 0.0

    for pos in open_positions:
        if pos.current_price is None:
            warnings.append(f"missing_mtm_price:{pos.order_id[:12]}")
            continue
        if pos.entry_price <= 0 or pos.entry_price >= 1.0:
            warnings.append(f"bad_entry_price:{pos.order_id[:12]}:{pos.entry_price}")
            continue
        if pos.side not in ("YES", "NO"):
            warnings.append(f"unknown_side:{pos.order_id[:12]}:{pos.side}")
            continue

        shares = pos.stake / pos.entry_price

        if pos.side == "YES":
            equity_per_share = pos.current_price
        else:  # NO
            equity_per_share = max(0.0, 1.0 - pos.current_price)

        equity = shares * equity_per_share
        unrealized += equity - pos.stake

    mtm_equity = wallet_balance + frozen_stake + unrealized

    return BankrollMtMResult(
        realized_balance=wallet_balance,
        unrealized_pnl=unrealized,
        mtm_equity=mtm_equity,
        warnings=warnings,
    )
