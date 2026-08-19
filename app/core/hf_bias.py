"""HF-calibrated structural biases для prediction markets.

Источник: HF Bias Verification (2026-04-13), 283,287 resolved markets,
min_volume $1,000, VWAP-based implied probability vs actual resolution.

Используется contra-bias pipeline для расчёта p_win без oracle_score.
"""

from __future__ import annotations

# always_no_wr: доля рынков в категории где NO побеждает (YES не наступает).
# Это = 1 - actual_yes_rate из HF verification.
# Используется как p_win для always-NO стратегий.
CATEGORY_NO_WIN_RATE: dict[str, float] = {
    "weather": 0.831,
    "culture": 0.721,
    "sports": 0.667,
    "politics": 0.659,
    "geopolitics": 0.645,
    "tech & ai": 0.623,
    "other": 0.588,
    "finance": 0.535,
    "crypto": 0.532,
    "minute markets": 0.509,
}

# Минимальный NO WR для генерации сигналов.
# Ниже этого порога edge слишком мал после fees.
MIN_NO_WIN_RATE = 0.53

# Категории где contra-bias НЕ генерирует сигналы
# (NO WR < MIN или отдельный pipeline).
EXCLUDED_CATEGORIES: frozenset[str] = frozenset(
    {
        "minute markets",  # 50.9% — нет edge после fees
    }
)

# Horizon-adjusted NO WR (из H4 verification).
# Используется для horizon-aware EV calculation.
HORIZON_NO_WIN_RATE: dict[str, float] = {
    "<1d": 0.514,
    "1-3d": 0.543,
    "3-7d": 0.639,
    "7-14d": 0.594,
    "14-30d": 0.648,
    "30-90d": 0.740,
    "90-365d": 0.784,
}


def get_no_win_rate(category: str, horizon_days: float | None = None) -> float:
    """Получить calibrated NO win rate для категории.

    Если horizon_days указан, используется geometric mean
    category bias × horizon bias (нормализованный на global).
    """
    cat_lower = category.lower() if category else "other"
    cat_wr = CATEGORY_NO_WIN_RATE.get(cat_lower, CATEGORY_NO_WIN_RATE["other"])

    if horizon_days is None:
        return cat_wr

    # Horizon adjustment: blend category WR с horizon WR
    global_no_wr = 0.578  # global NO WR из H1 (1 - 42.2%)
    if horizon_days < 1:
        h_wr = HORIZON_NO_WIN_RATE["<1d"]
    elif horizon_days < 3:
        h_wr = HORIZON_NO_WIN_RATE["1-3d"]
    elif horizon_days < 7:
        h_wr = HORIZON_NO_WIN_RATE["3-7d"]
    elif horizon_days < 14:
        h_wr = HORIZON_NO_WIN_RATE["7-14d"]
    elif horizon_days < 30:
        h_wr = HORIZON_NO_WIN_RATE["14-30d"]
    elif horizon_days < 90:
        h_wr = HORIZON_NO_WIN_RATE["30-90d"]
    else:
        h_wr = HORIZON_NO_WIN_RATE["90-365d"]

    # Geometric mean normalization:
    # adjusted = cat_wr × (h_wr / global_no_wr)
    # Clamp to [0.50, 0.95]
    adjusted = cat_wr * (h_wr / global_no_wr)
    return max(0.50, min(0.95, adjusted))
