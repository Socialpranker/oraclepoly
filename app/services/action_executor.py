"""Bounded L2 executor (Phase 3) — единственная точка превращения действия в эффект.

Спека: docs/superpowers/specs/2026-08-01-phase3-bounded-executor-design.md §3, §4.

Whitelist по kind (§2 п.3): адаптеров ровно два — pause_detector и reduce_cap;
неизвестный kind не доходит до кода эффекта. Это буквальная реализация
«least privilege + whitelist» инварианта §4.5 родительской спеки — ревьюеру
достаточно прочитать один файл, чтобы увидеть ВСЁ, что агентский контур способен
изменить в проде: два UPDATE по detector_state, других таблиц и операций нет.

Гейты здесь НЕ проверяются — они в app/analytics/action_gates.py и вызываются
раньше (telegram.py). Executor исполняет уже разрешённое.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import structlog

from app.analytics.action_gates import KIND_PAUSE_DETECTOR, KIND_REDUCE_CAP
from app.services.database import execute

logger = structlog.get_logger()

# Свой UPDATE, а не core.detector_state.mark_paused: тот возвращает None и глотает
# 'UPDATE 0', из-за чего пауза несуществующего детектора выглядела бы как успех.
# Менять общий примитив нельзя — он используется demote_alerts_monitor.
_PAUSE_DETECTOR_SQL = """
UPDATE detector_state
   SET stage = 'paused',
       paused_reason = $2,
       updated_at = now()
 WHERE detector = $1
"""

_REDUCE_CAP_SQL = """
UPDATE detector_state
   SET live_stake_cap_usd = $2,
       updated_at = now()
 WHERE detector = $1
"""


@dataclass(frozen=True)
class ExecutionResult:
    ok: bool
    effect: dict = field(default_factory=dict)
    error: str | None = None


async def _pause_detector(payload: dict) -> ExecutionResult:
    detector = payload["detector"]
    if not isinstance(detector, str) or not detector:
        return ExecutionResult(False, error="bad_payload")
    reason = str(payload.get("reason", "advisory L2"))
    status = await execute(_PAUSE_DETECTOR_SQL, detector, reason[:500])
    if not status.endswith(" 1"):
        return ExecutionResult(False, error="detector_not_found")
    return ExecutionResult(True, {"detector": detector, "new_stage": "paused", "reason": reason})


async def _reduce_cap(payload: dict) -> ExecutionResult:
    detector = payload["detector"]
    if not isinstance(detector, str) or not detector:
        return ExecutionResult(False, error="bad_payload")
    new_cap = float(payload["new_cap_usd"])
    # isfinite, а не только `<= 0`: float("nan") <= 0 и float("inf") <= 0 оба False,
    # то есть nan/inf прошли бы в live_stake_cap_usd и испортили сайзинг молча.
    if not math.isfinite(new_cap):
        return ExecutionResult(False, error="invalid_cap")
    if new_cap <= 0:
        # Ноль/отрицательное — это не «урезание», а поломка сайзинга. Для полной
        # остановки есть pause_detector.
        return ExecutionResult(False, error="invalid_cap")
    status = await execute(_REDUCE_CAP_SQL, detector, new_cap)
    if not status.endswith(" 1"):
        return ExecutionResult(False, error="detector_not_found")
    return ExecutionResult(True, {"detector": detector, "new_cap_usd": new_cap})


# Адаптера close_position здесь НЕТ намеренно (решение владельца 2026-08-01):
# policy engine агрегирует по детектору и не знает order_db_id, поэтому вызвать
# такой адаптер было бы неоткуда. Необратимое действие не получает разрешения
# «про запас» — закрытие позиции остаётся в контуре close_pos: (аудит — задача 5).
_ADAPTERS = {
    KIND_PAUSE_DETECTOR: _pause_detector,
    KIND_REDUCE_CAP: _reduce_cap,
}


async def execute_action(kind: str, payload: dict) -> ExecutionResult:
    """Исполнить разрешённое действие. Никогда не бросает — отказ возвращается значением.

    Вебхук Telegram не должен падать из-за сбоя адаптера: пользователю нужен внятный
    toast, а строке в agent_actions — статус 'failed' (спека §5).
    """
    adapter = _ADAPTERS.get(kind)
    if adapter is None:
        logger.warning("l2_unknown_kind", kind=kind)
        return ExecutionResult(False, error="unknown_kind")
    try:
        result = await adapter(payload)
    except KeyError as e:
        logger.warning("l2_bad_payload", kind=kind, missing=str(e))
        return ExecutionResult(False, error="bad_payload")
    except Exception as e:  # noqa: BLE001 — любой сбой адаптера обязан стать 'failed', а не 500
        logger.exception("l2_adapter_failed", kind=kind)
        return ExecutionResult(False, error=str(e))
    logger.info("l2_adapter_done", kind=kind, ok=result.ok)
    return result
