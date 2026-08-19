"""
8-уровневая система тиров китов.

Два измерения:
- Transaction tier: тир одной сделки по amount
- Whale tier: постоянный тир кита по avg_bet_size (с low-data cap)
"""

from typing import Literal

WhaleTier = Literal[
    "shrimp",
    "crab",
    "fish",
    "dolphin",
    "shark",
    "whale",
    "orca",
    "leviathan",
]

TIER_THRESHOLDS: list[tuple[WhaleTier, float, float]] = [
    ("shrimp", 5_000, 7_500),
    ("crab", 7_500, 10_000),
    ("fish", 10_000, 20_000),
    ("dolphin", 20_000, 50_000),
    ("shark", 50_000, 100_000),
    ("whale", 100_000, 250_000),
    ("orca", 250_000, 1_000_000),
    ("leviathan", 1_000_000, float("inf")),
]

TIER_ORDER: dict[str, int] = {name: i for i, (name, _, _) in enumerate(TIER_THRESHOLDS)}

LOW_DATA_CAP: WhaleTier = "fish"
LOW_DATA_THRESHOLD = 3

TIER_LABELS: dict[WhaleTier, str] = {
    "shrimp": "\U0001f990 Shrimp",
    "crab": "\U0001f980 Crab",
    "fish": "\U0001f41f Fish",
    "dolphin": "\U0001f42c Dolphin",
    "shark": "\U0001f988 Shark",
    "whale": "\U0001f40b Whale",
    "orca": "\U0001f531 Orca",
    "leviathan": "\U0001f451 Leviathan",
}


def _resolve_tier(amount: float) -> WhaleTier:
    """Определить тир по сумме. Итерирует от конца (leviathan) к началу."""
    for name, low, _ in reversed(TIER_THRESHOLDS):
        if amount >= low:
            return name
    return "shrimp"


def get_transaction_tier(amount: float) -> WhaleTier:
    """Тир одной сделки по сумме. Fallback: shrimp для amount < 5000."""
    if amount <= 0:
        return "shrimp"
    return _resolve_tier(amount)


def get_whale_tier(avg_bet_size: float | None, bet_count: int) -> WhaleTier:
    """
    Тир кита по avg_bet_size.

    Guard: при bet_count < LOW_DATA_THRESHOLD — cap на LOW_DATA_CAP (fish).
    При avg_bet_size = None или 0 → shrimp.
    """
    if not avg_bet_size or avg_bet_size <= 0:
        return "shrimp"

    tier = _resolve_tier(avg_bet_size)

    if bet_count < LOW_DATA_THRESHOLD:
        cap_idx = TIER_ORDER[LOW_DATA_CAP]
        tier_idx = TIER_ORDER[tier]
        if tier_idx > cap_idx:
            return LOW_DATA_CAP

    return tier


def tier_index(tier: str) -> int:
    """Числовой индекс тира для сравнений. -1 для неизвестного."""
    return TIER_ORDER.get(tier, -1)
