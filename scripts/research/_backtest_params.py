"""Shared backtest constants for all canonical research scripts.

ROI formula (per-notional, canonical across all detectors):
    notional = stake / cost  (= stake × 1/entry_price)
    WIN:  net_pnl = payout - fee_round_trip - slippage_abs
    LOSS: net_pnl = -(stake + fee_entry + slippage_abs)
    ROI_net = net_pnl / notional × 100

For aggregate WR-based scripts (D1-D6 Scheme A):
    gross_roi = (WR/100 - avg_cost) / avg_cost * 100
    net_roi   = (WR/100 * (1 - FEE_ROUND_TRIP_PCT/100) - cost_net) / cost_net * 100
    where cost_net = avg_cost + SLIPPAGE_PP/100

For per-row scripts (D7b, D10 Scheme B):
    raw_roi = (1/cost - 1) if win else -1   # per-share, same as per-notional at stake=1
    net_roi = avg(raw_roi) - FEE_ROUND_TRIP_PCT/100 - SLIPPAGE_PP/100/avg_cost
"""

# =================================================================
# Polymarket fees — DO NOT SET TO 0.0
# =================================================================
# UPDATED 2026-05-13. Полный обзор см. memory feedback_polymarket_fees_critical.md.
#
# CRITICAL: НИКОГДА не set DEFAULT_FEE_PCT = 0.0
#   - Polymarket НЕ бесплатен с 2026-03-30
#   - Historical orderfilled.parquet показывает ~0% потому что fees pre-March 2026
#   - Production code в app/services/fee_calculator.py имеет правильную модель
#
# Per-category peak (taker):
#   Crypto/Minute Markets: 1.80%   Sports/Economics: 1.50%
#   Mentions/Culture/Weather/Other: 1.25%
#   Finance/Politics/Tech: 1.00%   Geopolitics: 0% (only fee-free)
#
# Formula (production): fee = amount × rate × (p×(1-p))^1
#   peak at p=0.50, declines toward 0 at extremes (0.01/0.99)
#
# Makers: FREE + 20-25% rebate (50% для Finance launch)
# Source: https://docs.polymarket.com/trading/fees

# Default mid-category peak (используется fallback)
DEFAULT_FEE_PCT = 1.25
FEE_ROUND_TRIP_PCT = DEFAULT_FEE_PCT * 2  # 2.5% conservative

# Sanity check: блокируем accidental fee=0
assert DEFAULT_FEE_PCT >= 0.5, (
    "FEE TOO LOW! Polymarket charges 1.00-1.80% taker fees per category. "
    "Never set DEFAULT_FEE_PCT < 0.5. See feedback_polymarket_fees_critical.md"
)


def effective_taker_fee_pct(category: str | None, price: float) -> float:
    """Per-trade taker fee при given price.

    Использует production fee_calculator для consistency.
    Returns percentage (0-2), не fraction.
    """
    from app.services.fee_calculator import estimate_trading_fee
    fee_dollars = estimate_trading_fee(1.0, price, category or "other")
    return fee_dollars * 100.0  # convert to percentage

# Slippage per detector (pp = percentage points on cost)
# Based on observed book depth and zone price range
SLIPPAGE_PP: dict[str, float] = {
    "D1": 2.0,   # whale-following, mid-liquidity markets
    "D3": 3.0,   # cross-market arb, wider spread
    "D4": 4.0,   # small markets, thin book
    "D5": 2.0,   # long-horizon extreme, bigger markets
    "D6": 2.0,   # same as D5 (D5 subset)
    "D7b": 2.0,  # high-zone NO, competitive markets
    "D10b": 3.0, # election ladder, illiquid rungs
}

# Stake assumptions for $/day projections
STAKE_USD_AT_1K = 5.0    # $1K bankroll × 0.5%
STAKE_USD_AT_5K = 25.0   # $5K bankroll × 0.5%

# Canonical HF dataset span (markets from 2024-01-01 to ~2026-04-30)
BACKTEST_DAYS = 821
