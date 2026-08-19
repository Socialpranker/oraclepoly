"""Классификатор поведенческих сегментов китов.

Чистая логика без I/O. Все данные приходят через параметры.
Двухуровневая классификация:
  Level 1 (classify_whale): кошелёк → wallet_class (cluster|fresh|smart_money|experienced|None)
  Level 2 (assign_segment): транзакция → конкретный сегмент или None
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# ─── Константы ──────────────────────────────────────────────────────────

FRESH_MAX_AGE_DAYS: int = 14
FRESH_BIG_MIN_AMOUNT: float = 5_000.0
FRESH_BIG_MAX_BET_COUNT: int = 5
SWARM_MIN_TX_COUNT: int = 3
SWARM_MAX_AVG_AMOUNT: float = 1_000.0
CATEGORY_MIN_BETS: int = 10
CATEGORY_MIN_WR: float = 0.65
PROVEN_MIN_ROI: float = 10.0
PROVEN_MIN_WR: float = 0.55
PROVEN_MIN_BET_COUNT: int = 10
CLUSTER_MIN_SCORE: float = 0.5


# ─── SegmentConfig ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class SegmentConfig:
    """Конфигурация симуляции для сегмента."""

    bank: float
    kelly_mult: float
    max_bet_pct: float
    drawdown_stop: float


SEGMENT_NAMES: list[str] = ["cluster", "fresh_big", "fresh_swarm", "smart_money", "category_spec", "proven"]

SEGMENT_CONFIGS: dict[str, SegmentConfig] = {
    "cluster": SegmentConfig(bank=500, kelly_mult=1.0, max_bet_pct=0.10, drawdown_stop=0.15),
    "fresh_big": SegmentConfig(bank=750, kelly_mult=0.75, max_bet_pct=0.08, drawdown_stop=0.15),
    "fresh_swarm": SegmentConfig(bank=500, kelly_mult=0.5, max_bet_pct=0.05, drawdown_stop=0.15),
    "smart_money": SegmentConfig(bank=1250, kelly_mult=0.5, max_bet_pct=0.05, drawdown_stop=0.20),
    "category_spec": SegmentConfig(bank=750, kelly_mult=0.4, max_bet_pct=0.04, drawdown_stop=0.20),
    "proven": SegmentConfig(bank=1250, kelly_mult=0.25, max_bet_pct=0.03, drawdown_stop=0.25),
}


# ─── Level 1: classify_whale ────────────────────────────────────────────


def classify_whale(
    whale_data: dict,
    *,
    now: datetime | None = None,
) -> str | None:
    """Определяет wallet_class кита по приоритету.

    Порядок (высший → низший):
      1. cluster (coordination_score > 0.5)
      2. smart_money (is_smart_money == True)
      3. fresh (wallet_age < 14 дней)
      4. experienced (wallet_age >= 14d AND bet_count >= 10)
      5. None (не квалифицирован)

    Args:
        whale_data: словарь с полями whale_id, created_at, bet_count,
                    is_smart_money, coordination_score, roi, win_rate
        now: текущее время (для тестов), по умолчанию UTC now

    Returns:
        wallet_class строка или None
    """
    if now is None:
        now = datetime.now(UTC)

    coordination_score = whale_data.get("coordination_score", 0.0)
    if coordination_score > CLUSTER_MIN_SCORE:
        return "cluster"

    if whale_data.get("is_smart_money", False):
        return "smart_money"

    created_at = whale_data.get("created_at")
    if created_at is None:
        return None

    # Вычисляем возраст кошелька
    if isinstance(created_at, str):
        try:
            created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None

    wallet_age = now - created_at
    is_fresh = wallet_age < timedelta(days=FRESH_MAX_AGE_DAYS)

    if is_fresh:
        return "fresh"

    bet_count = whale_data.get("bet_count", 0)
    if bet_count >= PROVEN_MIN_BET_COUNT:
        return "experienced"

    return None


# ─── Level 2: assign_segment ────────────────────────────────────────────


def assign_segment(
    wallet_class: str | None,
    whale_data: dict,
    tx: dict,
    swarm_groups: dict,
    whale_category_stats: dict,
) -> str | None:
    """Определяет конкретный сегмент для транзакции.

    Args:
        wallet_class: результат classify_whale
        whale_data: метаданные кита
        tx: транзакция (whale_id, market_id, outcome, amount, category)
        swarm_groups: dict[(whale_id, market_id, outcome)] → {count, avg_amount}
        whale_category_stats: dict[whale_id][category] → {win_rate, bets}

    Returns:
        Название сегмента или None (транзакция пропускается)
    """
    if wallet_class is None:
        return None

    if wallet_class == "cluster":
        return "cluster"

    if wallet_class == "smart_money":
        return "smart_money"

    if wallet_class == "fresh":
        return _assign_fresh_segment(whale_data, tx, swarm_groups)

    if wallet_class == "experienced":
        return _assign_experienced_segment(whale_data, tx, whale_category_stats)

    return None


def _assign_fresh_segment(
    whale_data: dict,
    tx: dict,
    swarm_groups: dict,
) -> str | None:
    """Сегментация fresh кошелька: fresh_big или fresh_swarm."""
    bet_count = whale_data.get("bet_count", 0)
    amount = tx.get("amount", 0.0)

    # fresh_big: мало ставок + крупная сумма
    if bet_count < FRESH_BIG_MAX_BET_COUNT and amount >= FRESH_BIG_MIN_AMOUNT:
        return "fresh_big"

    # fresh_swarm: координированные мелкие ставки
    swarm_key = (tx.get("whale_id"), tx.get("market_id"), tx.get("outcome"))
    group = swarm_groups.get(swarm_key)
    if group is not None:
        if group.get("count", 0) >= SWARM_MIN_TX_COUNT and group.get("avg_amount", float("inf")) < SWARM_MAX_AVG_AMOUNT:
            return "fresh_swarm"

    return None


def _assign_experienced_segment(
    whale_data: dict,
    tx: dict,
    whale_category_stats: dict,
) -> str | None:
    """Сегментация experienced кошелька: category_spec или proven."""
    whale_id = whale_data.get("whale_id", "")
    category = tx.get("category", "")

    # category_spec: экспертиза в конкретной категории
    whale_cats = whale_category_stats.get(whale_id, {})
    cat_data = whale_cats.get(category)
    if cat_data is not None:
        cat_wr = cat_data.get("win_rate", 0.0)
        cat_bets = cat_data.get("bets", 0)
        if cat_wr >= CATEGORY_MIN_WR and cat_bets >= CATEGORY_MIN_BETS:
            return "category_spec"

    # proven: стабильный ROI + win rate на уровне кошелька
    roi = whale_data.get("roi", 0.0)
    win_rate = whale_data.get("win_rate", 0.0)
    if roi > PROVEN_MIN_ROI and win_rate > PROVEN_MIN_WR:
        return "proven"

    return None


# ─── classify_batch ──────────────────────────────────────────────────────


def classify_batch(
    *,
    whale_txs: list[dict],
    whale_meta: dict[str, dict],
    swarm_groups: dict,
    whale_category_stats: dict,
    cluster_scores: dict[str, float],
    now: datetime | None = None,
) -> dict[str, list[dict]]:
    """Группирует все транзакции по сегментам.

    Args:
        whale_txs: список транзакций
        whale_meta: dict[whale_id] → whale_data
        swarm_groups: dict[(whale_id, market_id, outcome)] → {count, avg_amount}
        whale_category_stats: dict[whale_id][category] → {win_rate, bets}
        cluster_scores: dict[whale_id] → coordination_score (override)
        now: текущее время (для тестов)

    Returns:
        dict[segment_name] → list[tx] — только транзакции с определённым сегментом
    """
    if now is None:
        now = datetime.now(UTC)

    # Кэш wallet_class по whale_id (Level 1 вычисляется один раз)
    wallet_class_cache: dict[str, str | None] = {}
    result: dict[str, list[dict]] = defaultdict(list)

    for tx in whale_txs:
        whale_id = tx.get("whale_id")
        if whale_id is None:
            continue

        whale_data = whale_meta.get(whale_id)
        if whale_data is None:
            continue

        # Level 1: wallet class (с кэшированием)
        if whale_id not in wallet_class_cache:
            # Применяем cluster_scores override
            enriched = dict(whale_data)
            if whale_id in cluster_scores:
                enriched["coordination_score"] = cluster_scores[whale_id]
            wallet_class_cache[whale_id] = classify_whale(enriched, now=now)

        wallet_class = wallet_class_cache[whale_id]

        # Level 2: segment assignment
        # Для classify, нужен enriched whale_data (с cluster_scores override)
        enriched_whale = dict(whale_data)
        if whale_id in cluster_scores:
            enriched_whale["coordination_score"] = cluster_scores[whale_id]

        segment = assign_segment(wallet_class, enriched_whale, tx, swarm_groups, whale_category_stats)

        if segment is not None:
            result[segment].append(tx)

    return dict(result)
