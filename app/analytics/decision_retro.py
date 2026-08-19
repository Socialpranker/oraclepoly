"""Чистая логика quarterly-ретро журнала решений C4 (Phase 2).

Без I/O — SQL и рассылка в app/jobs/decision_retro_quarterly.py.
Образец разделения: app/analytics/improvement_brief.py + app/jobs/improvement_brief_weekly.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

_OUTCOME_LABELS = {
    "improved": "✅ лучше",
    "unchanged": "➖ без изменений",
    "worsened": "❌ хуже",
    "not_applicable": "🚫 нечем сверить",
}


@dataclass(frozen=True)
class RetroRow:
    """Строка агрегата: сколько исходов данного типа в данном scope."""

    scope: str
    outcome: str
    n: int


def quarter_label(d: date) -> str:
    """'Q3 2026' — квартал даты."""
    return f"Q{(d.month - 1) // 3 + 1} {d.year}"


def quarter_bounds(d: date) -> tuple[date, date]:
    """Полуинтервал [начало квартала, начало следующего) — для WHERE ... >= $1 AND < $2."""
    q_start_month = (d.month - 1) // 3 * 3 + 1
    start = date(d.year, q_start_month, 1)
    if q_start_month == 10:
        return start, date(d.year + 1, 1, 1)
    return start, date(d.year, q_start_month + 3, 1)


def format_retro(rows: list[RetroRow], n_open: int, label: str) -> str:
    """TG-текст дайджеста. Пустой журнал — валидный вход (спека §4.4)."""
    head = f"🧾 <b>Ретро решений — {label}</b>"
    if not rows and n_open == 0:
        return f"{head}\n\nЗакрытых ожиданий нет, открытых нет."

    lines = [head, ""]
    if rows:
        total = sum(r.n for r in rows)
        lines.append(f"Закрыто ожиданий: <b>{total}</b>")
        by_scope: dict[str, list[RetroRow]] = {}
        for r in rows:
            by_scope.setdefault(r.scope, []).append(r)
        for scope in sorted(by_scope):
            parts = ", ".join(
                f"{_OUTCOME_LABELS.get(r.outcome, r.outcome)}: {r.n}"
                for r in sorted(by_scope[scope], key=lambda x: x.outcome)
            )
            lines.append(f"• <code>{scope}</code> — {parts}")
    else:
        lines.append("Закрытых ожиданий нет.")

    lines.append("")
    lines.append(f"Открытых ожиданий (ждут due_date): <b>{n_open}</b>")
    return "\n".join(lines)
