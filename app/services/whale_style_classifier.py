"""Классификатор стратегических стилей китов.

Чистая логика без I/O. Все данные приходят через WhaleProfile.
3-я ось классификации: tier (размер) + segment (происхождение) + style (стратегия).

Waterfall: фильтры шума → стратегические → temporal.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import mean as _mean
from statistics import median, stdev
from typing import Literal

WhaleStyle = Literal[
    "edge_trader",
    "asymmetric",
    "event_trader",
    "bonder",
    "speed_trader",
    "lp_maker",
    "arbitrageur",
    "specialist",
]

MIN_BETS_FOR_STYLE = 5
BONDER_PRICE_THRESHOLD = 0.90
BONDER_RATIO_THRESHOLD = 0.70
BONDER_MEDIAN_THRESHOLD = 0.92
EDGE_MIN_BETS = 10
EDGE_WR_LOW = 0.60
EDGE_WR_HIGH = 0.85
ASYMMETRIC_MIN_BETS = 15
ASYMMETRIC_MAX_WR = 0.40
ASYMMETRIC_TAIL_MULT = 5.0
SPECIALIST_MIN_CAT_BETS = 10
SPECIALIST_MIN_CAT_WR = 0.65
SPECIALIST_MIN_DELTA_WR = 0.10
LP_MAKER_MIN_RATIO = 0.60
LP_MAKER_MIN_BETS = 10
SPEED_MAX_REACTION_MINUTES = 30.0
EVENT_MIN_BETS = 15
EVENT_CONCENTRATION_THRESHOLD = 0.60
EVENT_TOP_DAYS_RATIO = 0.20


@dataclass(frozen=True)
class StyleConfig:
    """Параметры copy-trading simulation для стиля."""

    kelly_mult: float
    max_bet_pct: float


STYLE_CONFIGS: dict[str, StyleConfig] = {
    "edge_trader": StyleConfig(kelly_mult=0.50, max_bet_pct=0.05),
    "asymmetric": StyleConfig(kelly_mult=0.15, max_bet_pct=0.02),
    "bonder": StyleConfig(kelly_mult=0.80, max_bet_pct=0.15),
    "specialist": StyleConfig(kelly_mult=0.40, max_bet_pct=0.04),
    "event_trader": StyleConfig(kelly_mult=0.30, max_bet_pct=0.05),
    "speed_trader": StyleConfig(kelly_mult=0.35, max_bet_pct=0.04),
    "lp_maker": StyleConfig(kelly_mult=0.0, max_bet_pct=0.0),
    "arbitrageur": StyleConfig(kelly_mult=0.0, max_bet_pct=0.0),
}


@dataclass(frozen=True)
class WhaleProfile:
    """Входные данные для классификации стиля."""

    whale_id: str
    win_rate: float
    bet_count: int
    pnl_history: list[float]
    entry_prices: list[float]
    is_arbitrageur: bool
    category_stats: dict  # {cat: {wins: int, losses: int}}
    avg_reaction_minutes: float | None
    maker_ratio: float | None


def classify_style(profile: WhaleProfile) -> tuple[WhaleStyle | None, float]:
    """Определяет стратегический стиль кита.

    Returns:
        (style, confidence) или (None, 0.0) при недостатке данных.
    """
    if profile.bet_count < MIN_BETS_FOR_STYLE:
        return None, 0.0

    # Фильтры шума — первыми
    if profile.is_arbitrageur:
        return "arbitrageur", 0.95

    if result := _is_bonder(profile):
        return result

    if result := _is_lp_maker(profile):
        return result

    # Стратегические архетипы
    if result := _is_edge_trader(profile):
        return result

    if result := _is_asymmetric(profile):
        return result

    if result := _is_specialist(profile):
        return result

    # Temporal
    if result := _is_speed_trader(profile):
        return result

    if result := _is_event_trader(profile):
        return result

    return None, 0.0


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


def _is_bonder(p: WhaleProfile) -> tuple[WhaleStyle, float] | None:
    if not p.entry_prices:
        return None
    bonded = sum(1 for price in p.entry_prices if price > BONDER_PRICE_THRESHOLD)
    ratio = bonded / len(p.entry_prices)
    if ratio < BONDER_RATIO_THRESHOLD:
        return None
    med = median(p.entry_prices)
    if med < BONDER_MEDIAN_THRESHOLD:
        return None
    return "bonder", _clamp01(ratio)


def _is_edge_trader(p: WhaleProfile) -> tuple[WhaleStyle, float] | None:
    if p.bet_count < EDGE_MIN_BETS:
        return None
    if not (EDGE_WR_LOW <= p.win_rate <= EDGE_WR_HIGH):
        return None
    if len(p.pnl_history) < 2:
        return None
    m = _mean(p.pnl_history)
    if m <= 0:
        return None
    s = stdev(p.pnl_history)
    if s >= m:
        return None
    confidence = 1.0 - s / max(m, 1e-6)
    return "edge_trader", _clamp01(confidence)


def _is_asymmetric(p: WhaleProfile) -> tuple[WhaleStyle, float] | None:
    if p.bet_count < ASYMMETRIC_MIN_BETS:
        return None
    if p.win_rate >= ASYMMETRIC_MAX_WR:
        return None
    if len(p.pnl_history) < 2:
        return None
    m = _mean(p.pnl_history)
    if m <= 0:
        return None
    mx = max(p.pnl_history)
    if mx <= 0:
        return None
    abs_values = [abs(v) for v in p.pnl_history]
    med_abs = median(abs_values) if abs_values else 0
    if med_abs <= 0:
        return None
    if mx <= ASYMMETRIC_TAIL_MULT * med_abs:
        return None
    confidence = mx / (ASYMMETRIC_TAIL_MULT * med_abs)
    return "asymmetric", _clamp01(confidence)


def _is_specialist(p: WhaleProfile) -> tuple[WhaleStyle, float] | None:
    if not p.category_stats:
        return None
    best_delta = 0.0
    for _cat, data in p.category_stats.items():
        wins = int(data.get("wins", 0))
        losses = int(data.get("losses", 0))
        total = wins + losses
        if total < SPECIALIST_MIN_CAT_BETS:
            continue
        wr = wins / total
        if wr >= SPECIALIST_MIN_CAT_WR:
            delta = wr - p.win_rate
            if delta > best_delta:
                best_delta = delta
    if best_delta < SPECIALIST_MIN_DELTA_WR:
        return None
    confidence = best_delta / 0.30
    return "specialist", _clamp01(confidence)


def _is_lp_maker(p: WhaleProfile) -> tuple[WhaleStyle, float] | None:
    if p.maker_ratio is None:
        return None
    if p.bet_count < LP_MAKER_MIN_BETS:
        return None
    if p.maker_ratio <= LP_MAKER_MIN_RATIO:
        return None
    return "lp_maker", _clamp01(p.maker_ratio)


def _is_speed_trader(p: WhaleProfile) -> tuple[WhaleStyle, float] | None:
    if p.avg_reaction_minutes is None:
        return None
    if p.avg_reaction_minutes >= SPEED_MAX_REACTION_MINUTES:
        return None
    confidence = 1.0 - p.avg_reaction_minutes / SPEED_MAX_REACTION_MINUTES
    return "speed_trader", _clamp01(confidence)


def _is_event_trader(p: WhaleProfile) -> tuple[WhaleStyle, float] | None:
    if p.bet_count < EVENT_MIN_BETS:
        return None
    if len(p.pnl_history) < EVENT_MIN_BETS:
        return None
    abs_pnl = [abs(v) for v in p.pnl_history]
    total_abs = sum(abs_pnl)
    if total_abs <= 0:
        return None
    # Сортируем по убыванию, берём top 20%
    sorted_abs = sorted(abs_pnl, reverse=True)
    top_count = max(1, int(len(sorted_abs) * EVENT_TOP_DAYS_RATIO))
    top_sum = sum(sorted_abs[:top_count])
    concentration = top_sum / total_abs
    if concentration < EVENT_CONCENTRATION_THRESHOLD:
        return None
    return "event_trader", _clamp01(concentration)
