"""Signal Router — утилиты для работы с multi-source сигналами.

Dedup по market_id внутри одного source (убрать дубликаты event_id→market_id).
Cross-source dedup НЕ делается: каждый source подаёт сигналы своим стратегиям
независимо. Overlap позиций контролируется на уровне position opening
(UNIQUE constraint strategy_key + market_id + snapshot_date).
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger()


def dedup_signals(signals: list[dict]) -> list[dict]:
    """Дедуплицировать сигналы по market_id внутри одного source.

    При коллизии (один market_id дважды) оставляет сигнал с большим EV.

    Args:
        signals: список сигналов от одного источника

    Returns:
        Список без дубликатов market_id.
    """
    if not signals:
        return []

    best: dict[str, dict] = {}
    for sig in signals:
        mid = sig.get("market_id", "")
        if not mid:
            continue
        existing = best.get(mid)
        if existing is None or sig.get("ev", 0) > existing.get("ev", 0):
            best[mid] = sig

    return list(best.values())
