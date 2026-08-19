"""Tests для app/services/pnl_formulas.py — single source of truth for PnL formula.

Verifies that compute_net_pnl matches v2_execution_resolve._compute_settlement_pnl
для одинаковых inputs (parity-проверка).
"""

from __future__ import annotations

import pytest

from app.jobs.v2_execution_resolve import _compute_settlement_pnl
from app.services.pnl_formulas import (
    compute_gross_pnl,
    compute_net_pnl,
    entry_price_for_side,
)


class TestEntryPriceForSide:
    def test_yes_side_returns_market_price(self) -> None:
        assert entry_price_for_side("YES", 0.7) == 0.7

    def test_no_side_returns_complement(self) -> None:
        assert entry_price_for_side("NO", 0.7) == pytest.approx(0.3)

    def test_extreme_yes(self) -> None:
        assert entry_price_for_side("YES", 0.95) == 0.95

    def test_extreme_no(self) -> None:
        assert entry_price_for_side("NO", 0.95) == pytest.approx(0.05)


class TestComputeNetPnl:
    def test_expired_returns_zero(self) -> None:
        assert compute_net_pnl(
            outcome="EXPIRED", side="YES", market_yes_price=0.5, stake_usd=100, category="Sports"
        ) == 0.0

    def test_invalid_outcome_returns_zero(self) -> None:
        assert compute_net_pnl(
            outcome="UNKNOWN", side="YES", market_yes_price=0.5, stake_usd=100, category="Sports"
        ) == 0.0

    def test_zero_entry_returns_zero(self) -> None:
        # NO @ 1.0 → entry = 0
        assert compute_net_pnl(
            outcome="WIN", side="NO", market_yes_price=1.0, stake_usd=100, category="Sports"
        ) == 0.0

    def test_win_yes_positive_pnl(self) -> None:
        # YES @ 0.5 + WIN: payout = 1/0.5 - 1 = 1.0; gross = 100 * 1 = 100
        # минус fees + slippage → ожидаем positive < 100
        pnl = compute_net_pnl(
            outcome="WIN", side="YES", market_yes_price=0.5, stake_usd=100, category="Sports"
        )
        assert 0 < pnl < 100

    def test_loss_yes_negative_pnl(self) -> None:
        pnl = compute_net_pnl(
            outcome="LOSS", side="YES", market_yes_price=0.5, stake_usd=100, category="Sports"
        )
        # LOSS: -(stake + fee + slippage) → должен быть < -100
        assert pnl < -100

    def test_no_side_uses_inverse_price(self) -> None:
        # NO @ 0.9 = entry 0.1; WIN payout 1/0.1 - 1 = 9
        pnl = compute_net_pnl(
            outcome="WIN", side="NO", market_yes_price=0.9, stake_usd=100, category="Sports"
        )
        # Большой payout
        assert pnl > 800

    def test_compute_net_pnl_win_no_slippage(self) -> None:
        """WIN: net = stake*payout - fee, slippage не вычитается (mirror-инвариант).

        NO @ market_yes=0.60 → entry=0.40, payout=1/0.40-1=1.5, gross=15.0.
        net = 15 - fee (fee небольшая, положительная); без slippage net > 14.
        """
        net = compute_net_pnl(
            outcome="WIN", side="NO", market_yes_price=0.60,
            stake_usd=10.0, category="crypto",
        )
        assert net > 14.0


class TestComputeGrossPnl:
    def test_win_yes(self) -> None:
        # YES @ 0.5 + WIN: 100 * (1/0.5 - 1) = 100
        assert compute_gross_pnl(
            outcome="WIN", side="YES", market_yes_price=0.5, stake_usd=100
        ) == pytest.approx(100.0)

    def test_loss_returns_negative_stake(self) -> None:
        assert compute_gross_pnl(
            outcome="LOSS", side="YES", market_yes_price=0.5, stake_usd=100
        ) == -100.0

    def test_expired_returns_zero(self) -> None:
        assert compute_gross_pnl(
            outcome="EXPIRED", side="YES", market_yes_price=0.5, stake_usd=100
        ) == 0.0


class TestParityWithSettlementResolve:
    """compute_net_pnl должен соответствовать _compute_settlement_pnl для same inputs.

    Resolve использует fill_price (после slippage), мы используем market_yes_price
    (counterfactual). Если fill_price == market_yes_price (no slippage случай) —
    результаты должны совпадать.
    """

    @pytest.mark.parametrize(
        "outcome,side,price,stake,category",
        [
            ("WIN", "YES", 0.5, 100.0, "Sports"),
            ("LOSS", "YES", 0.5, 100.0, "Sports"),
            ("WIN", "NO", 0.9, 50.0, "Politics"),
            ("LOSS", "NO", 0.9, 50.0, "Politics"),
            ("WIN", "YES", 0.85, 200.0, "Crypto"),
            ("LOSS", "NO", 0.10, 25.0, "Other"),
        ],
    )
    def test_parity_same_price(
        self, outcome: str, side: str, price: float, stake: float, category: str
    ) -> None:
        """Когда fill_price == market_yes_price entry-side, формулы должны совпадать."""
        # _compute_settlement_pnl получает fill_price = entry-side adjusted price
        # (для NO это 1 - market_yes_price = entry)
        entry = price if side == "YES" else 1.0 - price

        ours = compute_net_pnl(
            outcome=outcome, side=side, market_yes_price=price,
            stake_usd=stake, category=category,
        )
        theirs = _compute_settlement_pnl(
            stake=stake, fill_price=entry, outcome=outcome, category=category,
        )["net_pnl"]

        # Tolerance 1e-2 (rounding в _compute_settlement_pnl до 4 знаков)
        assert abs(ours - theirs) < 0.01, (
            f"Divergence: outcome={outcome} side={side} price={price} stake={stake} "
            f"category={category}: ours={ours} theirs={theirs}"
        )
