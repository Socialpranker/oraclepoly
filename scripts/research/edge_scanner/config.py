"""Auto Edge Scanner — constants & configuration.

All magic numbers live here. PR review required to change features list.
"""

from __future__ import annotations

# ─── Data sources ────────────────────────────────────────────────────────────

ML_FEATURES_PATH = "/mnt/data/canonical/ml_features_v2.parquet"


# ─── Numeric features to sweep ───────────────────────────────────────────────
# Pre-registered list. Adding/removing requires PR review (avoid silent
# expansion that causes p-hacking).
#
# Excludes:
#   - target column (yes_won)
#   - identifier columns (condition_id, event_id, anchor_hours, category)
#   - leakage-prone (yes_price_*h after anchor — historical fwd prices)
#   - features that are core inputs to existing detectors with high LGB gain
#     (yes_price_at_anchor, vwap_asof, price_max_asof, price_min_asof —
#     these are the dominant signal; sweeping them re-discovers known territory)

NUMERIC_FEATURES: list[str] = [
    # Wallet quality (PIT-correct, from PR #417)
    "smart_money_taker_share",
    "top_decile_taker_share",
    "taker_pnl_weighted_usd_24h",
    "taker_roi_weighted_24h",
    "taker_volume_usd_total_24h",
    "n_takers_24h_before",
    "smart_money_taker_volume_usd",
    "top_decile_taker_volume_usd",
    # Whale flow / concentration
    "whale_buy_pressure_24h",
    "whale_taker_volume",
    "whale_taker_count",
    "taker_hhi_proxy_24h",
    "repeat_taker_ratio_24h",
    "retail_taker_avg_size_24h",
    # Buy/sell imbalance
    "buy_imbalance_asof",
    "buy_ratio_24h_before_anchor",
    "buy_ratio_72h_before_anchor",
    # Volatility
    "realized_vol_24h",
    "realized_vol_72h",
    "max_drawdown_24h_pct",
    "vol_24h_concentration",
    "vol_24h_pct_asof",
    # Microstructure proxies
    "vpin_proxy_lifetime",
    "kyle_lambda_proxy",
    "liquidity_quality_proxy",
    # Drift
    "drift_72h_to_anchor",
    "drift_7d_to_anchor",
    # Event-level
    "event_arb_distance_yes",
    "event_largest_market_share",
    "event_thinnest_market_vol",
    "event_yes_sum",
    "event_total_lifetime_volume",
    # Horizon / timing
    "horizon_days",
]


# ─── Quantile thresholds per feature ──────────────────────────────────────────
# 5 percentiles × 2 directions (>, <) = 10 thresholds per feature.

QUANTILES = [0.10, 0.25, 0.50, 0.75, 0.90]
THRESHOLD_OPS = [">", "<"]


# ─── Price zones ──────────────────────────────────────────────────────────────

ZONES: list[tuple[str, float, float]] = [
    ("longshot", 0.10, 0.30),
    ("low", 0.30, 0.50),
    ("mid", 0.50, 0.70),
    ("high", 0.70, 0.85),
    ("extreme", 0.85, 0.97),
]


# ─── Anchors ──────────────────────────────────────────────────────────────────

ANCHORS = [4, 24, 72]


# ─── Categories ───────────────────────────────────────────────────────────────
# None = ALL categories (ignore filter).

CATEGORIES: list[str | None] = [
    "Politics",
    "Crypto",
    "Sports",
    "Other",
    "Tech & AI",
    "Finance",
    None,  # ALL
]


# ─── Sides ────────────────────────────────────────────────────────────────────

SIDES = ["NO", "YES"]


# ─── Strict gate thresholds ───────────────────────────────────────────────────

MIN_N = 500
MIN_N_UNIQUE_MARKETS = 300
MIN_NET_ROI = 0.05  # 5% post fees + slip + haircut
MIN_MIRROR_DIFF_PP = 10.0  # 10pp asymmetric edge required
MIN_YEAR_SAMPLES = 100  # min n per year for split check
FDR_ALPHA = 0.05  # Benjamini-Hochberg q-value threshold


# ─── Economics ────────────────────────────────────────────────────────────────

DEFAULT_FEE_PCT = 1.5  # one-side; total drag = 2 * 1.5 = 3.0%
SLIPPAGE_PP_BY_ZONE = {
    "longshot": 4.0,
    "low": 2.5,
    "mid": 2.0,
    "high": 2.5,
    "extreme": 4.0,
}
HONEST_HAIRCUT = 0.5  # production filter mismatch + post-V2 unverified
DEFAULT_BANKROLL_USD = 5000.0
DEFAULT_AVG_STAKE_USD = 25.0  # 0.5% bankroll per trade


# ─── Dedup cutoffs ────────────────────────────────────────────────────────────

OVERLAP_SUBSET_THRESHOLD = 0.80
OVERLAP_NOVEL_THRESHOLD = 0.50


# ─── TG alert caps ────────────────────────────────────────────────────────────

MAX_NOVEL_ALERTS_PER_RUN = 3
MAX_TOTAL_MESSAGES_PER_RUN = 5
SUPPRESS_REPEAT_DAYS = 90  # don't re-alert same signature within N days


# ─── Output paths ─────────────────────────────────────────────────────────────

MD_REPORT_DIR = "docs/research"
CSV_OUTPUT_DIR = "outputs"


# ─── Backtest period ──────────────────────────────────────────────────────────

BACKTEST_DAYS = 821  # canonical 821-day window
