"""Slippage model — моделирует bid/ask spread + market impact при dry entry.

Две модели:

1. **Legacy per-category** (`get_spread_estimate`, `apply_slippage`) — фиксированный
   half-spread по категории, калиброван по живым fills 2026-04-06/13. Используется
   в production callers (ev_sizer, detector_signals_resolve, v2_execution_resolve).

2. **Post-V2 per-asset** (`apply_realistic_slippage`) — нормировка slippage к цене
   ПОКУПАЕМОГО актива (cost_no = 1 - yes_price для NO, cost_yes = yes_price для YES).
   Это критично для extreme zones: при cost_no=0.05, "2pp full price" = 40% удорожание.
   Калибровано по эмпирике d7b_zone_split_test_2026-05-02 (см. memory).

Эмпирическая формула:
    cost_real = cost_quoted * (1 + baseline_frac + impact_K * utilization)
    utilization = stake_usd / (depth_l5_usd * 0.5)  # 0.5 — половина книги на нашей стороне

Параметры калиброваны на live fills:
    baseline_frac = 0.02   (2% от цены покупаемого актива — bid-ask half-spread MM)
    impact_K      = 0.05   (5% удорожание при полном съедании top-5 levels)
"""

from __future__ import annotations

_HALF_SPREAD: dict[str, float] = {
    "sports": 0.020,
    "politics": 0.025,
    "crypto": 0.030,
    "culture": 0.030,
    "finance": 0.025,
    "tech & ai": 0.030,
    "weather": 0.035,
    "world & economy": 0.030,
    "geopolitics": 0.030,
}

_DEFAULT_SPREAD = 0.030
_TAKER_MULTIPLIER = 1.5

# Per-asset slippage model (post-V2 calibrated)
_BASELINE_FRAC_OF_COST = 0.02  # 2% bid-ask half-spread от цены покупаемого ассета
_IMPACT_K = 0.05  # 5% удорожание при utilization=1.0 (полное съедание top-5)
_DEPTH_L5_FRACTION_PER_SIDE = 0.5  # половина top-5 books лежит на каждой стороне
_NEG_RISK_PREMIUM_FRAC = 0.01  # +1% от cost для NegRisk markets (MM floor effect)
_LIFETIME_VOL_TO_DEPTH_L5 = 0.01  # depth_l5 ≈ lifetime_volume × 0.01 как middle proxy
_VOLUME_24H_TO_DEPTH_L5 = 0.05  # depth_l5 ≈ volume_24h × 0.05 как литература


def get_spread_estimate(category: str) -> float:
    return _HALF_SPREAD.get(category.lower(), _DEFAULT_SPREAD)


def apply_slippage(
    side: str,
    entry_price: float,
    category: str,
    execution_mode: str = "maker",
) -> float:
    """Ухудшить entry_price на half-spread (buyer платит больше). Clamp [0.01, 0.99]."""
    spread = get_spread_estimate(category)
    if execution_mode == "taker":
        spread *= _TAKER_MULTIPLIER
    slipped = entry_price + spread
    return max(0.01, min(slipped, 0.99))


def _resolve_depth_l5(
    book_depth_l5_usd: float | None,
    volume_24h_usd: float | None,
    lifetime_volume_usd: float | None,
) -> float:
    """Возвращает best-available depth_l5 estimate.

    Приоритет: actual depth → volume_24h × 0.05 → lifetime × 0.01 → fallback $1.
    """
    if book_depth_l5_usd is not None and book_depth_l5_usd > 0:
        return book_depth_l5_usd
    if volume_24h_usd and volume_24h_usd > 0:
        return volume_24h_usd * _VOLUME_24H_TO_DEPTH_L5
    if lifetime_volume_usd and lifetime_volume_usd > 0:
        return lifetime_volume_usd * _LIFETIME_VOL_TO_DEPTH_L5
    return 1.0


def per_asset_slippage_frac(
    stake_usd: float,
    cost_quoted: float,
    book_depth_l5_usd: float | None = None,
    volume_24h_usd: float | None = None,
    lifetime_volume_usd: float | None = None,
    is_neg_risk: bool = False,
) -> float:
    """Slippage как ДОЛЯ от цены покупаемого ассета.

    Для NO buy: cost_quoted = 1 - yes_price.
    Для YES buy: cost_quoted = yes_price.

    Returns:
        slip_frac — например 0.04 = +4% удорожание cost.
        cost_real = cost_quoted * (1 + slip_frac).
    """
    if stake_usd <= 0 or cost_quoted <= 0:
        return _BASELINE_FRAC_OF_COST

    depth_l5 = _resolve_depth_l5(book_depth_l5_usd, volume_24h_usd, lifetime_volume_usd)
    depth_our_side = max(depth_l5 * _DEPTH_L5_FRACTION_PER_SIDE, 1.0)
    utilization = min(stake_usd / depth_our_side, 1.0)
    slip_frac = _BASELINE_FRAC_OF_COST + _IMPACT_K * utilization
    if is_neg_risk:
        slip_frac += _NEG_RISK_PREMIUM_FRAC
    return slip_frac


def apply_realistic_slippage(
    side: str,
    entry_price: float,
    stake_usd: float,
    book_depth_l5_usd: float | None = None,
    volume_24h_usd: float | None = None,
    lifetime_volume_usd: float | None = None,
    is_neg_risk: bool = False,
) -> float:
    """Ухудшить entry_price на per-asset slippage. Clamp [0.01, 0.99].

    side='YES' → buy YES at entry_price → cost_quoted = entry_price.
    side='NO'  → buy NO at (1 - entry_price) → cost_quoted = 1 - entry_price.

    Для consistency с downstream код возвращаем эквивалентный yes_price:
        - YES buy: yes_price_real = entry_price * (1 + slip_frac)
        - NO buy:  cost_no_real = (1 - entry_price) * (1 + slip_frac)
                   yes_price_real = 1 - cost_no_real
    """
    side_upper = side.upper()
    if side_upper == "YES":
        cost_quoted = entry_price
    elif side_upper == "NO":
        cost_quoted = 1.0 - entry_price
    else:
        raise ValueError(f"side must be 'YES' or 'NO', got {side!r}")

    slip_frac = per_asset_slippage_frac(
        stake_usd=stake_usd,
        cost_quoted=cost_quoted,
        book_depth_l5_usd=book_depth_l5_usd,
        volume_24h_usd=volume_24h_usd,
        lifetime_volume_usd=lifetime_volume_usd,
        is_neg_risk=is_neg_risk,
    )
    cost_real = cost_quoted * (1.0 + slip_frac)
    if side_upper == "YES":
        yes_price_real = cost_real
    else:
        yes_price_real = 1.0 - cost_real

    return max(0.01, min(yes_price_real, 0.99))
