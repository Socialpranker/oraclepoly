"""Unit tests для backtest metrics — Wilson formula + aggregations."""

from __future__ import annotations

import pytest

from app.backtest.metrics import (
    aggregate_by_category,
    aggregate_roi,
    signal_fee_pct,
    signal_pnl_pct,
    signal_slippage_pct,
    top_losses_by_confidence,
    wilson_score_interval,
)
from app.backtest.schema import FeeMode, SignalOutcome, SlippageMode

# === wilson_score_interval ===


def test_wilson_zero_total() -> None:
    """nobs=0 → (0.0, 0.0) — no division by zero."""
    assert wilson_score_interval(0, 0) == (0.0, 0.0)


def test_wilson_5_of_10_matches_wikipedia() -> None:
    """Reference: 5/10 with 95% → ≈ (0.237, 0.763)."""
    lower, upper = wilson_score_interval(5, 10, 0.95)
    assert 0.230 < lower < 0.240
    assert 0.760 < upper < 0.770


def test_wilson_all_wins() -> None:
    """100/100 → upper bound ≈ 1.0, lower bound > 0.96."""
    lower, upper = wilson_score_interval(100, 100, 0.95)
    assert lower > 0.96
    assert upper <= 1.0


def test_wilson_all_losses() -> None:
    """0/100 → lower ≈ 0.0, upper > 0.0 but small."""
    lower, upper = wilson_score_interval(0, 100, 0.95)
    assert lower == 0.0
    assert 0.0 < upper < 0.05


def test_wilson_half_50_50() -> None:
    """50/100 → centered around 0.5."""
    lower, upper = wilson_score_interval(50, 100, 0.95)
    assert 0.40 < lower < 0.41
    assert 0.59 < upper < 0.60


def test_wilson_confidence_99_wider_than_95() -> None:
    """99% CI wider than 95% CI."""
    l95, u95 = wilson_score_interval(50, 100, 0.95)
    l99, u99 = wilson_score_interval(50, 100, 0.99)
    assert l99 < l95
    assert u99 > u95


def test_wilson_rejects_invalid_wins() -> None:
    with pytest.raises(ValueError):
        wilson_score_interval(11, 10, 0.95)
    with pytest.raises(ValueError):
        wilson_score_interval(-1, 10, 0.95)


def test_wilson_rejects_unsupported_confidence() -> None:
    with pytest.raises(ValueError):
        wilson_score_interval(5, 10, 0.97)  # not in lookup


# === aggregate_by_category ===


def _make_outcome(category: str, won: bool, confidence: float = 0.5) -> SignalOutcome:
    return SignalOutcome(
        market_id=f"m_{category}_{won}_{confidence}",
        event_id=None,
        category=category,
        side="YES",
        entry_price=0.97,
        fair_price=0.99,
        confidence=confidence,
        expected_edge_pct=2.0,
        hours_to_resolve=24,
        yes_won=1 if won else 0,
        won=won,
        question="test",
    )


def test_aggregate_empty() -> None:
    """No signals → empty dict."""
    result = aggregate_by_category([])
    assert result == {}


def test_aggregate_groups_by_category() -> None:
    outcomes = [
        _make_outcome("sports", True),
        _make_outcome("sports", True),
        _make_outcome("sports", False),
        _make_outcome("crypto", True),
    ]
    result = aggregate_by_category(outcomes)
    assert set(result.keys()) == {"sports", "crypto"}
    assert result["sports"].n_signals == 3
    assert result["sports"].n_wins == 2
    assert abs(result["sports"].win_rate - 2 / 3) < 1e-9
    assert result["crypto"].n_signals == 1
    assert result["crypto"].n_wins == 1


def test_aggregate_handles_none_category() -> None:
    """category=None becomes 'unknown'."""
    outcomes = [_make_outcome("", True)]
    result = aggregate_by_category(outcomes)
    assert "unknown" in result


# === top_losses_by_confidence ===


def test_top_losses_empty() -> None:
    assert top_losses_by_confidence([]) == []


def test_top_losses_filters_winners() -> None:
    """Only losses returned, even if winner has higher confidence."""
    outcomes = [
        _make_outcome("a", won=True, confidence=0.99),
        _make_outcome("b", won=False, confidence=0.5),
    ]
    result = top_losses_by_confidence(outcomes, limit=10)
    assert len(result) == 1
    assert result[0].confidence == 0.5
    assert result[0].won is False


def test_top_losses_sorted_descending_by_confidence() -> None:
    outcomes = [
        _make_outcome("a", won=False, confidence=0.3),
        _make_outcome("b", won=False, confidence=0.9),
        _make_outcome("c", won=False, confidence=0.5),
    ]
    result = top_losses_by_confidence(outcomes, limit=10)
    assert [o.confidence for o in result] == [0.9, 0.5, 0.3]


def test_top_losses_respects_limit() -> None:
    outcomes = [_make_outcome(f"c{i}", won=False, confidence=0.1 * i) for i in range(20)]
    result = top_losses_by_confidence(outcomes, limit=5)
    assert len(result) == 5


# === signal_pnl_pct + aggregate_roi ===


def _outcome(won: bool, entry: float, category: str = "x") -> SignalOutcome:
    return SignalOutcome(
        market_id="m",
        event_id=None,
        category=category,
        side="YES",
        entry_price=entry,
        fair_price=0.5,
        confidence=0.5,
        expected_edge_pct=1.0,
        hours_to_resolve=24,
        yes_won=1 if won else 0,
        won=won,
        question="test",
    )


def test_signal_pnl_loss_returns_neg100() -> None:
    assert signal_pnl_pct(_outcome(False, 0.5)) == -100.0


def test_signal_pnl_win_at_50_returns_100() -> None:
    """Entry 0.50, win → payoff $1 / $0.50 = 2x → +100%."""
    assert signal_pnl_pct(_outcome(True, 0.5)) == pytest.approx(100.0)


def test_signal_pnl_win_at_10_returns_900() -> None:
    """Entry 0.10, win → payoff 10x → +900%."""
    assert signal_pnl_pct(_outcome(True, 0.1)) == pytest.approx(900.0)


def test_signal_pnl_win_at_90_returns_11pct() -> None:
    """Entry 0.90, win → payoff ~1.11x → +11.11%."""
    assert signal_pnl_pct(_outcome(True, 0.9)) == pytest.approx(11.111, abs=0.01)


def test_signal_pnl_zero_entry_safe() -> None:
    assert signal_pnl_pct(_outcome(True, 0.0)) == 0.0


def test_aggregate_roi_empty() -> None:
    assert aggregate_roi([]) == (0.0, 0.0)


def test_aggregate_roi_net_minus_3pct() -> None:
    """5 wins @ 0.5 (+100% each) + 5 losses → avg +0%, net -3%."""
    outcomes = [_outcome(True, 0.5) for _ in range(5)] + [_outcome(False, 0.5) for _ in range(5)]
    gross, net = aggregate_roi(outcomes)
    assert gross == pytest.approx(0.0, abs=0.01)
    assert net == pytest.approx(-3.0, abs=0.01)


def test_aggregate_roi_underdog_strategy_positive() -> None:
    """Underdog example: entry 0.10, win rate 13% → +30% gross, +27% net.

    13 wins × 900% + 87 losses × -100% = 11700 - 8700 = +3000 / 100 = +30%.
    """
    outcomes = [_outcome(True, 0.1) for _ in range(13)] + [_outcome(False, 0.1) for _ in range(87)]
    gross, net = aggregate_roi(outcomes)
    assert gross == pytest.approx(30.0, abs=0.5)
    assert net == pytest.approx(27.0, abs=0.5)


# === Dynamic fees (FeeMode.DYNAMIC) ===


def test_signal_fee_pct_crypto_extreme_low() -> None:
    """D44 entry @ 0.97 Crypto: ≈ 0.21% (not flat 3%).

    100 × 0.072 × (0.97 × 0.03)^1 = 100 × 0.072 × 0.0291 = $0.2095 = 0.21%.
    """
    sig = _outcome(True, 0.97, category="crypto")
    fee = signal_fee_pct(sig, stake_usd=100.0)
    assert fee == pytest.approx(0.21, abs=0.01)


def test_signal_fee_pct_crypto_50pct_peak() -> None:
    """Crypto entry @ 0.50: peak fee.

    100 × 0.072 × 0.25 = $1.8 = 1.80%.
    """
    sig = _outcome(True, 0.50, category="crypto")
    fee = signal_fee_pct(sig, stake_usd=100.0)
    assert fee == pytest.approx(1.80, abs=0.01)


def test_signal_fee_pct_sports_lower_than_crypto() -> None:
    """Sports fee rate (3%) < Crypto (7.2%) — same price → smaller fee."""
    crypto_fee = signal_fee_pct(_outcome(True, 0.5, "crypto"))
    sports_fee = signal_fee_pct(_outcome(True, 0.5, "sports"))
    assert sports_fee < crypto_fee
    assert sports_fee == pytest.approx(0.75, abs=0.01)


def test_signal_fee_pct_geopolitics_zero() -> None:
    """Geopolitics — fee-free category."""
    sig = _outcome(True, 0.50, category="geopolitics")
    fee = signal_fee_pct(sig, stake_usd=100.0)
    assert fee == 0.0


def test_aggregate_roi_dynamic_lower_than_flat_in_extreme() -> None:
    """В extreme zone (entry 0.97) dynamic fees << flat 3%, поэтому Net ROI выше."""
    outcomes = [_outcome(True, 0.97, "crypto") for _ in range(95)] + [
        _outcome(False, 0.97, "crypto") for _ in range(5)
    ]
    _, net_flat = aggregate_roi(outcomes, fee_mode=FeeMode.FLAT)
    _, net_dynamic = aggregate_roi(outcomes, fee_mode=FeeMode.DYNAMIC)
    # Dynamic fees ~0.21%, flat 3%, разница ~2.79pp
    assert net_dynamic - net_flat == pytest.approx(2.79, abs=0.05)


# === Slippage (SlippageMode) ===


def test_signal_slippage_pct_none_zero() -> None:
    assert signal_slippage_pct(_outcome(True, 0.97, "crypto"), SlippageMode.NONE) == 0.0


def test_signal_slippage_pct_baseline_2pct() -> None:
    assert signal_slippage_pct(_outcome(True, 0.97, "crypto"), SlippageMode.BASELINE) == 2.0


def test_signal_slippage_pct_realistic_crypto_3pct() -> None:
    """Crypto half_spread = 0.030 → 3%."""
    assert signal_slippage_pct(_outcome(True, 0.97, "crypto"), SlippageMode.REALISTIC) == pytest.approx(3.0)


def test_signal_slippage_pct_realistic_sports_2pct() -> None:
    """Sports half_spread = 0.020 → 2%."""
    assert signal_slippage_pct(_outcome(True, 0.97, "sports"), SlippageMode.REALISTIC) == pytest.approx(2.0)


def test_aggregate_roi_dynamic_with_realistic_slippage() -> None:
    """Net = gross - fee_dynamic - slippage_realistic. Все три выкручены."""
    outcomes = [_outcome(True, 0.97, "crypto") for _ in range(95)] + [
        _outcome(False, 0.97, "crypto") for _ in range(5)
    ]
    gross, net = aggregate_roi(
        outcomes,
        fee_mode=FeeMode.DYNAMIC,
        slippage_mode=SlippageMode.REALISTIC,
    )
    # Sanity: gross > net (slippage haircut). Crypto slippage = 3%.
    assert (gross - net) == pytest.approx(3.21, abs=0.05)  # 0.21 fee + 3.0 slippage
