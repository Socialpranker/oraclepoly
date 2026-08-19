"""Пять гейтов L2-исполнения (Phase 3) — чистый слой без I/O.

Спека: docs/superpowers/specs/2026-08-01-phase3-bounded-executor-design.md §3, §5.

Порядок проверок ЗНАЧИМ: админ первым (чужому не сообщаем о состоянии системы),
дальше от дешёвого к дорогому. Любой отказ — fail-closed: действие не выполняется,
auto-approve по TTL запрещён (§2 п.4, задокументированный источник реальных потерь).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

KIND_PAUSE_DETECTOR = "pause_detector"
KIND_REDUCE_CAP = "reduce_cap"

# Whitelist §2 п.3. Расширение — только осознанной правкой + ревью.
# close_position НЕ здесь: у policy engine нет order_db_id (он агрегирует по детектору),
# потребителя у такого адаптера не было бы. Закрытие живёт в контуре close_pos:.
ALLOWED_KINDS: frozenset[str] = frozenset({KIND_PAUSE_DETECTOR, KIND_REDUCE_CAP})

# Тексты отказов для владельца (спека §5). Отказ обязан быть понятным,
# иначе кнопка воспринимается как сломанная.
REASON_TEXTS = {
    "not_admin": "⛔ Только для админов",
    "unknown_kind": "❌ Неизвестное действие — не выполнено",
    "expired": "⌛ Предложение просрочено (>24ч). Действие не выполнено — дождись свежего.",
    "already_handled": "✅ Уже сделано — повторное нажатие ничего не меняет.",
    "snapshot_changed": "🔄 Данные изменились с момента отправки. Действие не выполнено — проверь текущее состояние.",
    "dq_red": "🔴 Данные сейчас ненадёжны. Действие не выполнено — сначала разберись с качеством данных.",
}


def snapshot_hash(payload: Mapping) -> str:
    """Стабильный хэш снапшота. sort_keys — иначе порядок ключей давал бы ложные отказы.

    dict(payload) обязателен: `default=str` сериализует не-dict Mapping (например
    MappingProxyType из notification.ActionSpec) как repr-строку, БЕЗ сортировки
    ключей — тихо давая другой хэш для тех же данных. Это отклоняло бы каждое
    исполнение с текстом «данные изменились», и моки в тестах такого не поймали бы.
    """
    return hashlib.sha256(json.dumps(dict(payload), sort_keys=True, default=str).encode()).hexdigest()


@dataclass(frozen=True)
class ActionRequest:
    """Строка agent_actions + факт админства, приведённые к типам."""

    kind: str
    status: str
    expires_at: datetime
    payload_hash: str
    is_admin: bool


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reason: str | None = None  # ключ REASON_TEXTS; None когда allowed=True

    @property
    def text(self) -> str:
        """Текст для toast'а владельцу."""
        if self.allowed:
            return "⚡ Сделано"
        return REASON_TEXTS.get(self.reason or "", "❌ Действие не выполнено")


def check_gates(
    req: ActionRequest,
    now: datetime,
    dq_verdict: str,
    current_snapshot: dict,
) -> GateDecision:
    """Пять гейтов §3. Первый несработавший определяет отказ.

    dq_verdict — вердикт СВЕЖЕГО DQ-run на момент клика, не на момент создания
    proposal (§8 риск 6): иначе гарантия «DQ RED → approval invalidated» не выполняется.
    """
    if not req.is_admin:
        return GateDecision(False, "not_admin")
    if req.kind not in ALLOWED_KINDS:
        return GateDecision(False, "unknown_kind")
    if req.status != "proposed":
        return GateDecision(False, "already_handled")
    if req.expires_at <= now:
        return GateDecision(False, "expired")
    if snapshot_hash(current_snapshot) != req.payload_hash:
        return GateDecision(False, "snapshot_changed")
    if dq_verdict == "RED":
        return GateDecision(False, "dq_red")
    return GateDecision(True)
