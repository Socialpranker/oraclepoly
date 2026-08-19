"""MtM-aware bankroll расчёт.

Phase 1 Stage 1. См.
docs/superpowers/plans/2026-05-risk-engine-hardening/01-mtm-daily-pnl.md.

Semantic invariants (унифицированы с position_manager._unrealized_pnl_pct,
SSoT: app/jobs/position_manager.py:145-172):
  - entry_price хранится в координатах нашей стороны:
      YES side → entry_price = YES price at entry
      NO  side → entry_price = NO  price at entry = 1 − yes_price_at_fill
  - current_price = canonical YES price (markets.current_price)
  - YES equity per share = current_price
  - NO  equity per share = 1 − current_price
"""

from __future__ import annotations

import pytest

from app.core.bankroll_mtm import MtMPositionSnapshot, compute_bankroll_with_mtm


def test_mtm_with_single_open_position_50pct_loss():
    """YES @ entry=0.40, current YES=0.20 → equity = stake × (0.20/0.40) = $150.
    Unrealized = $150 − $300 = -$150. MtM equity = wallet + frozen + unrealized."""
    snapshots = [
        MtMPositionSnapshot(
            order_id="abc",
            stake=300.0,
            entry_price=0.40,
            current_price=0.20,
            side="YES",
        )
    ]
    result = compute_bankroll_with_mtm(
        wallet_balance=700.0,
        frozen_stake=300.0,
        open_positions=snapshots,
    )
    assert result.realized_balance == 700.0
    assert result.unrealized_pnl == pytest.approx(-150.0)
    # mtm = 700 (free) + 300 (frozen) + (-150 unrealized) = 850
    assert result.mtm_equity == pytest.approx(850.0)


def test_mtm_with_winning_position():
    """YES @ entry=0.10, current YES=0.30 → equity = stake × 3.0 = $600.
    Unrealized = $600 − $200 = +$400."""
    snapshots = [
        MtMPositionSnapshot(
            order_id="def",
            stake=200.0,
            entry_price=0.10,
            current_price=0.30,
            side="YES",
        )
    ]
    result = compute_bankroll_with_mtm(
        wallet_balance=800.0, frozen_stake=200.0, open_positions=snapshots
    )
    assert result.unrealized_pnl == pytest.approx(400.0)
    # mtm = 800 + 200 + 400 = 1400
    assert result.mtm_equity == pytest.approx(1400.0)


def test_mtm_no_positions_equals_wallet_plus_frozen():
    """Нет open positions → unrealized=0, mtm = wallet + frozen."""
    result = compute_bankroll_with_mtm(
        wallet_balance=1000.0, frozen_stake=0.0, open_positions=[]
    )
    assert result.mtm_equity == 1000.0
    assert result.unrealized_pnl == 0.0


def test_mtm_no_price_data_treats_position_at_entry():
    """current_price=None (WS/REST не дал данные) → conservative: unrealized=0,
    flag warning. Не паниковать."""
    snapshots = [
        MtMPositionSnapshot(
            order_id="ghi",
            stake=100.0,
            entry_price=0.50,
            current_price=None,
            side="YES",
        )
    ]
    result = compute_bankroll_with_mtm(
        wallet_balance=900.0, frozen_stake=100.0, open_positions=snapshots
    )
    assert result.unrealized_pnl == 0.0
    # mtm = 900 + 100 + 0 = 1000 (no MtM penalty без данных)
    assert result.mtm_equity == 1000.0
    assert any("missing_mtm_price" in w for w in result.warnings)


def test_mtm_no_side_breakeven():
    """NO @ entry=0.60 (купили NO когда YES был 0.40), current YES=0.40 →
    NO equity per share = 1 − 0.40 = 0.60 = entry. PnL = 0."""
    snapshots = [
        MtMPositionSnapshot(
            order_id="jkl",
            stake=100.0,
            entry_price=0.60,
            current_price=0.40,  # YES = 0.40 → NO = 0.60 = entry
            side="NO",
        )
    ]
    result = compute_bankroll_with_mtm(
        wallet_balance=900.0, frozen_stake=100.0, open_positions=snapshots
    )
    assert result.unrealized_pnl == pytest.approx(0.0, abs=0.01)


def test_mtm_no_side_winning():
    """NO @ entry=0.60, current YES=0.20 → NO equity = 0.80. shares = 100/0.60.
    equity_total = (100/0.60) × 0.80 ≈ $133.33. PnL = +$33.33."""
    snapshots = [
        MtMPositionSnapshot(
            order_id="mno",
            stake=100.0,
            entry_price=0.60,
            current_price=0.20,
            side="NO",
        )
    ]
    result = compute_bankroll_with_mtm(
        wallet_balance=900.0, frozen_stake=100.0, open_positions=snapshots
    )
    assert result.unrealized_pnl == pytest.approx(33.33, abs=0.01)


def test_mtm_no_side_losing():
    """NO @ entry=0.60, current YES=0.80 → NO equity = 0.20.
    shares = 100/0.60 ≈ 166.67. equity = 166.67 × 0.20 ≈ $33.33. PnL = -$66.67."""
    snapshots = [
        MtMPositionSnapshot(
            order_id="pqr",
            stake=100.0,
            entry_price=0.60,
            current_price=0.80,
            side="NO",
        )
    ]
    result = compute_bankroll_with_mtm(
        wallet_balance=900.0, frozen_stake=100.0, open_positions=snapshots
    )
    assert result.unrealized_pnl == pytest.approx(-66.67, abs=0.01)


def test_mtm_mixed_positions():
    """Несколько positions: суммируем unrealized."""
    snapshots = [
        MtMPositionSnapshot(
            order_id="yes_winner", stake=100.0, entry_price=0.20, current_price=0.40, side="YES"
        ),
        MtMPositionSnapshot(
            order_id="no_loser", stake=100.0, entry_price=0.30, current_price=0.80, side="NO"
        ),
    ]
    result = compute_bankroll_with_mtm(
        wallet_balance=800.0, frozen_stake=200.0, open_positions=snapshots
    )
    # YES winner: equity = 100 * (0.40/0.20) = 200. PnL = +100.
    # NO loser: shares = 100/0.30 = 333.33. equity = 333.33 * (1-0.80) = 66.67. PnL = -33.33.
    # Total unrealized: +100 - 33.33 ≈ 66.67
    assert result.unrealized_pnl == pytest.approx(66.67, abs=0.01)
    # mtm = 800 + 200 + 66.67 = 1066.67
    assert result.mtm_equity == pytest.approx(1066.67, abs=0.01)


def test_mtm_bad_entry_price_skipped():
    """entry_price <= 0 или ≥ 1 — невалидный, skip с warning."""
    snapshots = [
        MtMPositionSnapshot(
            order_id="bad1", stake=100.0, entry_price=0.0, current_price=0.5, side="YES"
        ),
        MtMPositionSnapshot(
            order_id="bad2", stake=100.0, entry_price=1.5, current_price=0.5, side="YES"
        ),
    ]
    result = compute_bankroll_with_mtm(
        wallet_balance=900.0, frozen_stake=200.0, open_positions=snapshots
    )
    assert result.unrealized_pnl == 0.0
    assert len(result.warnings) == 2
    assert all("bad_entry_price" in w for w in result.warnings)


def test_mtm_unknown_side_skipped():
    """side ∉ {YES, NO} → skip с warning."""
    snapshots = [
        MtMPositionSnapshot(
            order_id="weird", stake=100.0, entry_price=0.5, current_price=0.5, side="MAYBE"
        ),
    ]
    result = compute_bankroll_with_mtm(
        wallet_balance=900.0, frozen_stake=100.0, open_positions=snapshots
    )
    assert result.unrealized_pnl == 0.0
    assert any("unknown_side" in w for w in result.warnings)


def test_mtm_extreme_yes_price_clipped_to_no():
    """NO equity при current YES=0.99 → NO equity = 0.01 (не отрицательное)."""
    snapshots = [
        MtMPositionSnapshot(
            order_id="extreme", stake=100.0, entry_price=0.50, current_price=0.99, side="NO"
        ),
    ]
    result = compute_bankroll_with_mtm(
        wallet_balance=900.0, frozen_stake=100.0, open_positions=snapshots
    )
    # shares = 100/0.50 = 200. equity = 200 * 0.01 = $2. PnL = -$98
    assert result.unrealized_pnl == pytest.approx(-98.0, abs=0.01)
