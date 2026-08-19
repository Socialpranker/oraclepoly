"""Классификация rejection_reason: транзиентная причина = несостоявшаяся попытка (можно ретраить,
пока сигнал свеж). Терминальная = сигнал плохой по существу, ретрай бессмыслен. (audit топ-4)"""

from __future__ import annotations

TRANSIENT_PREFIXES: tuple[str, ...] = (
    "kill_switch",  # флап флага — снимется
    "daemon_paused",  # пауза детектора демоном (Task 4)
    "max_inflight_orders",  # слот освободится
    "max_open_positions",
    "daily_loss",  # сбросится в полночь
    "market_closed_or_empty_book",  # мгновенно пустой book / открытый breaker
    "stale_mtm",
    "place_error",  # сетевые/5xx; двусмысленный timeout закрыт сверкой с CLOB
)


def is_transient_reject(reason: str | None) -> bool:
    if not reason:
        return False
    return reason.startswith(TRANSIENT_PREFIXES)
