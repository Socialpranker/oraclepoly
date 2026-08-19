"""
SSoT модуль entry_price — единый источник истины для вычисления и валидации entry price.

entry_price (aka effective_entry, token_price) = цена токена для выбранной стороны:
- YES side: entry_price = market_price (YES probability)
- NO side: entry_price = 1 - market_price

Константы floor/ceiling централизованы здесь вместо дублирования по 3+ файлам.
"""

# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

ENTRY_FLOOR_YES: float = 0.15
"""YES < 15¢ — longshot Favourite-Longshot Bias."""

ENTRY_FLOOR_NO: float = 0.10
"""NO < 10¢ — unprofitable."""

ENTRY_FLOOR_ABSOLUTE: float = 0.03
"""Absolute minimum (execution_job)."""

ENTRY_FLOOR_CONTRA_BIAS_NO: float = 0.30
"""Contra-bias NO floor: бэктест 523K snapshots показал profit только при 0.30-0.70."""

ENTRY_FLOOR_FAVORITE_LONGSHOT_NO: float = 0.02
"""Favorite-longshot NO floor: edge именно в extreme zone (entry 2-10¢, market_price 90-98%).
HF backtest 2026-04-24: WR 57-59%, ROI 874-1240% на n=71-91. Bürgi/Deng/Whelan 2025."""

ENTRY_CEILING: float = 0.90
""">= 90¢ — unprofitable by Edge Map. Raised from 0.70: 73K+ resolved snapshots show WR 77-94% for entry 0.70-0.90 on NO signals (divergence < 0)."""

# ---------------------------------------------------------------------------
# Source-specific floors
# ---------------------------------------------------------------------------

_SOURCE_FLOORS: dict[str, dict[str, float]] = {
    "oracle": {"YES": ENTRY_FLOOR_YES, "NO": ENTRY_FLOOR_NO},
    "contra_bias": {"YES": ENTRY_FLOOR_YES, "NO": ENTRY_FLOOR_CONTRA_BIAS_NO},
    "weather": {"YES": ENTRY_FLOOR_ABSOLUTE, "NO": ENTRY_FLOOR_ABSOLUTE},
    "execution": {"YES": ENTRY_FLOOR_ABSOLUTE, "NO": ENTRY_FLOOR_ABSOLUTE},
    "favorite_longshot": {"YES": ENTRY_FLOOR_YES, "NO": ENTRY_FLOOR_FAVORITE_LONGSHOT_NO},
    "orderbook_imbalance": {"YES": ENTRY_FLOOR_ABSOLUTE, "NO": ENTRY_FLOOR_ABSOLUTE},
}

_DEFAULT_SOURCE = "oracle"

# ---------------------------------------------------------------------------
# Compute
# ---------------------------------------------------------------------------


def compute_effective_entry(side: str, market_price: float) -> float:
    """Вычислить effective entry price для данной стороны.

    Args:
        side: "YES" или "NO".
        market_price: рыночная цена YES-токена (probability 0..1).

    Returns:
        entry_price >= 0.01 (clamp снизу).
    """
    if side == "YES":
        return max(market_price, 0.01)
    # NO
    return max(1.0 - market_price, 0.01)


# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------


def validate_entry_price(side: str, entry_price: float, source: str = "oracle") -> tuple[bool, str]:
    """Проверить entry_price на допустимость (floor + ceiling).

    Args:
        side: "YES" или "NO".
        entry_price: цена токена для данной стороны.
        source: источник сигнала (oracle, contra_bias, weather, execution).

    Returns:
        (True, "") — допустимо.
        (False, reason) — отклонено с причиной.
    """
    floors = _SOURCE_FLOORS.get(source, _SOURCE_FLOORS[_DEFAULT_SOURCE])
    floor = floors.get(side, ENTRY_FLOOR_YES)

    if entry_price < floor:
        return False, f"entry_price {entry_price:.4f} ниже floor {floor} для {side} (source={source})"

    if entry_price >= ENTRY_CEILING:
        return False, f"entry_price {entry_price:.4f} >= ceiling {ENTRY_CEILING} (unprofitable)"

    return True, ""
