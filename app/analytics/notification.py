"""Единый контракт уведомления — спека 2026-08-04-notification-envelope-design.md §3.

Джоба отдаёт структуру, не текст. Форматирование — notification_render,
маршрутизация — notification_bus. Это единственный способ сделать правило
обязательным для будущих джоб: свободной строки в контракте просто нет.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class Severity(StrEnum):
    """Три уровня §4. Отдельным сообщением уходит только ACTION_REQUIRED."""

    ACTION_REQUIRED = "action_required"
    FYI = "fyi"
    TELEMETRY = "telemetry"


@dataclass(frozen=True)
class ActionSpec:
    """Действие для кнопки. kind обязан быть в action_gates.ALLOWED_KINDS —
    проверяется шиной при создании proposal, не здесь (контракт без I/O).

    payload/snapshot оборачиваются в MappingProxyType — настоящая неизменяемость,
    не только защита от переприсваивания атрибута. ВАЖНО: json.dumps(MappingProxyType)
    не бросает исключение (json сериализует его как Mapping), НО даёт другую строку,
    чем json.dumps(dict) — это МОЛЧА поменяет snapshot_hash(). Вызывающий код
    (в т.ч. action_gates.snapshot_hash()) обязан передавать dict(spec.payload) /
    dict(spec.snapshot), а не сам MappingProxyType, иначе хеш разъедется.
    """

    kind: str
    payload: Mapping[str, Any]
    snapshot: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))
        object.__setattr__(self, "snapshot", MappingProxyType(dict(self.snapshot)))


@dataclass(frozen=True)
class Notification:
    severity: Severity
    subject: str  # "detector:D38_resolution_lag" — ключ склейки и дедупа
    kind: str  # "edge_decay" — тип события
    headline: str
    what_happened: tuple[str, ...]
    what_if_ignored: str | None  # None когда честной цифры нет (§3)
    what_to_do: str | None
    actions: tuple[ActionSpec, ...] = ()
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Принимаем list на входе (естественно для джоб), храним tuple (настоящая
        # неизменяемость — frozen=True защищает только от переприсваивания атрибута).
        object.__setattr__(self, "what_happened", tuple(self.what_happened))
        object.__setattr__(self, "actions", tuple(self.actions))
        object.__setattr__(self, "evidence", tuple(self.evidence))

        if self.severity == Severity.ACTION_REQUIRED and not self.what_to_do:
            raise ValueError(
                f"ACTION_REQUIRED без what_to_do: {self.subject}|{self.kind}. "
                "Нечего делать — значит это FYI, а не действие."
            )

    def dedup_key(self) -> str:
        """Ключ дедупа/склейки. Только subject+kind: текст меняется от прогона
        к прогону (цифры плавают), и включение его в ключ сбрасывало бы cooldown
        на каждом изменении — рецидив инцидента 2026-08-01 (PR #741)."""
        return f"{self.subject}|{self.kind}"
