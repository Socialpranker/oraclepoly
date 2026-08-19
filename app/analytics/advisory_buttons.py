"""Чистый слой L1-кнопок advisory (Phase 2 C3b).

Сборка inline-клавиатуры и парсинг callback_data. Без side effects.
callback_data = "adv:{action}:{pid}", pid = proposal_id()[:16] (hex).
Спека: docs/superpowers/specs/2026-07-24-phase2-c3b-advisory-buttons-design.md §3.1.
"""

from __future__ import annotations

PREFIX = "adv"
ACK = "ack"
INVESTIGATE = "investigate"
REVIEW = "review"
EXECUTE = "execute"

_ACTIONS = (ACK, INVESTIGATE, REVIEW)
_LABELS = {ACK: "✅ Принял", INVESTIGATE: "🔍 Investigate", REVIEW: "📋 Request review"}

_EXECUTE_LABEL = "⚡ Сделать"
EXEC_PREFIX = "act"


def build_advisory_keyboard(pid: str, with_execute: bool = False) -> list[list[dict]]:
    """L1-кнопки; при with_execute — второй ряд с L2-кнопкой исполнения (Phase 3).

    with_execute=False по умолчанию: клавиатура Phase 2 не меняется там, где
    L2-действия нет (KEEP_SHADOW / INVESTIGATE_DATA).
    """
    rows = [[{"text": _LABELS[a], "callback_data": f"{PREFIX}:{a}:{pid}"} for a in _ACTIONS]]
    if with_execute:
        rows.append([{"text": _EXECUTE_LABEL, "callback_data": f"{EXEC_PREFIX}:{pid}"}])
    return rows


def parse_advisory_callback(data: str) -> tuple[str, str] | None:
    """'adv:action:pid' → (action, pid); None при битом формате / неизвестном action / пустом pid."""
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != PREFIX or parts[1] not in _ACTIONS or not parts[2]:
        return None
    return parts[1], parts[2]


def parse_action_callback(data: str) -> str | None:
    """'act:pid' → pid; None при битом формате."""
    parts = data.split(":")
    if len(parts) != 2 or parts[0] != EXEC_PREFIX or not parts[1]:
        return None
    return parts[1]
