"""TG-алерт adverse selection intel. Отделён от orchestration (§2 design doc).

Contract §3.4: каждая adverse-ячейка цитирует механизм каталога знаний как
пару (file:line + Источник). Механизм — market_economics.md (adverse selection,
confirmed) → memory temperature-markets-adverse-selection-2026-07-07.md.
"""

from __future__ import annotations

from app.analytics.adverse_selection import AdverseCell
from app.analytics.notification import Notification, Severity
from app.analytics.subject_labels import humanize_subject

_KNOWLEDGE_REF = (
    "docs/knowledge/market_economics.md (adverse selection, confirmed) "
    "→ memory temperature-markets-adverse-selection-2026-07-07.md"
)


def _cell_key(cell: AdverseCell) -> str:
    return f"{cell.detector}×{cell.category}"


def format_alert(cells: list[AdverseCell], added: list[str], removed: list[str]) -> str:
    """Сводный TG-алерт: дельты throttle + топ adverse-ячейки + gate-рекомендация."""
    adverse = sorted(
        (c for c in cells if c.is_adverse),
        key=lambda c: c.net_pnl,  # худшие по деньгам первыми
    )

    lines: list[str] = ["🎯 <b>Adverse Selection Intel</b>", ""]

    if added:
        lines.append(f"🆕 Новые adverse: {', '.join(added)}")
    if removed:
        lines.append(f"✅ Ушли из adverse: {', '.join(removed)}")
    if added or removed:
        lines.append("")

    lines.append(f"<b>Adverse detector×category ({len(adverse)}):</b>")
    for cell in adverse:
        temp = " 🌡 temperature (эталонный якорь)" if cell.is_temperature else ""
        winrate = cell.wins / cell.n_events if cell.n_events else 0.0
        lines.append(
            f"• <code>{_cell_key(cell)}</code>: net ${cell.net_pnl:.0f}, "
            f"winrate {winrate:.0%} (Wilson↓ {cell.wilson_lower:.2f}), N={cell.n_events}{temp}"
        )
        lines.append(f"  → рассмотреть блэклист {_cell_key(cell)} (human decision, gate не изменён)")

    lines.append("")
    lines.append(f"mechanism: {_KNOWLEDGE_REF}")
    return "\n".join(lines)


def _humanize_cell(cell: AdverseCell) -> str:
    """'D3_cross_mkt_arb×crypto' → 'D3 «Расхождение цен» на крипте'."""
    name = humanize_subject("detector:" + cell.detector)
    return f"{name} на «{cell.category}»"


def to_notifications(cells: list[AdverseCell], added: list[str], removed: list[str]) -> list[Notification]:
    """Изменение набора adverse-ячеек → Notification (спека §3).

    Появление и исчезновение разведены на РАЗНЫЕ kind по той же причине, что
    recovery у proxy_health_check: cooldown шины держит 25 часов по ключу
    (subject, kind), и общий kind означал бы, что «связка перестала быть
    убыточной» подавлено после «связка стала убыточной». Уход из adverse —
    хорошая новость без следующего шага, то есть FYI (§4).

    what_if_ignored несёт цифру, но НАЗЫВАЕТ ЕЁ ВИРТУАЛЬНОЙ: net_pnl считается
    по execution_mode='virtual' (см. _CELL_ROWS_SQL). Подать бумажный убыток как
    живые деньги — то же враньё, что выдумать цифру, только правдоподобнее.
    """
    out: list[Notification] = []
    adverse_by_key = {_cell_key(c): c for c in cells if c.is_adverse}

    if added:
        fresh = [adverse_by_key[k] for k in added if k in adverse_by_key]
        worst = min(fresh, key=lambda c: c.net_pnl, default=None)

        lines = []
        for cell in sorted(fresh, key=lambda c: c.net_pnl):
            winrate = cell.wins / cell.n_events if cell.n_events else 0.0
            lines.append(
                f"{_humanize_cell(cell)}: выигрывает {winrate:.0%} случаев из {cell.n_events}, "
                f"по бумажным сделкам это ${cell.net_pnl:.0f}."
            )
        lines.append("Это признак того, что мы заходим в сделку последними: цена уже учла то, чего мы ещё не знаем.")

        cost = None
        if worst is not None:
            cost = (
                f"Пока это виртуальные деньги: {_humanize_cell(worst)} набрал ${worst.net_pnl:.0f} "
                "на бумаге. Реальным этот убыток станет, если связку продвинуть в live."
            )

        out.append(
            Notification(
                severity=Severity.ACTION_REQUIRED,
                subject="analytics:adverse_selection",
                kind="adverse_added",
                headline="Появились связки, где мы систематически проигрываем",
                what_happened=lines,
                what_if_ignored=cost,
                what_to_do=(
                    "Посмотри на эти связки детектор+категория и реши, стоит ли их закрыть. "
                    "Автоматически ничего не отключено — гейт не тронут."
                ),
                actions=[],
                evidence=[
                    f"{_cell_key(c)}: net ${c.net_pnl:.0f}, Wilson↓ {c.wilson_lower:.2f}, N={c.n_events}" for c in fresh
                ]
                + [f"mechanism: {_KNOWLEDGE_REF}"],
            )
        )

    if removed:
        out.append(
            Notification(
                severity=Severity.FYI,
                subject="analytics:adverse_selection",
                kind="adverse_removed",
                headline="Связки перестали быть убыточными",
                what_happened=[f"Больше не в списке проблемных: {', '.join(removed)}."],
                what_if_ignored=None,
                what_to_do=None,
                actions=[],
                evidence=list(removed),
            )
        )

    return out
