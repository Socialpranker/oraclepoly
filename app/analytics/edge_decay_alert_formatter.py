"""Сборка TG-алерта для edge decay/regime monitor — app/jobs/edge_decay_monitor.py.

Отделён от orchestration ради независимого тестирования формата (много
edge-case на пустых/None-полях) без мока всей джобы, как решено в design doc.

is_decaying() — единственное место, определяющее "что считается decay"
(triggered И direction == "below" — обе метрики Wilson/Sortino "выше=лучше"
в этом проекте). И format_alert(), и app/jobs/edge_decay_monitor.py (для
решения писать ли agent_claims) используют эту же функцию — критерий не
дублируется в двух местах.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.analytics.changepoint import ChangepointResult
from app.analytics.edge_metrics import WindowMetrics
from app.analytics.notification import Notification, Severity


@dataclass(frozen=True)
class EntityDecayResult:
    entity_name: str
    entity_kind: str  # "detector" | "methodology"
    baseline: WindowMetrics
    recent: WindowMetrics
    wilson_changepoint: ChangepointResult
    sortino_changepoint: ChangepointResult


def is_decaying(entity: EntityDecayResult) -> bool:
    """Deterioration signal: CUSUM triggered И направление 'below' на Wilson или Sortino.

    Wilson win-rate и Sortino — обе "выше=лучше" метрики в этом проекте, поэтому
    'below' (recent ниже baseline) однозначно значит деградацию для обеих.
    """
    wilson_decaying = entity.wilson_changepoint.triggered and entity.wilson_changepoint.direction == "below"
    sortino_decaying = entity.sortino_changepoint.triggered and entity.sortino_changepoint.direction == "below"
    return wilson_decaying or sortino_decaying


_KIND_LABELS = {"detector": "детектор", "methodology": "методологический класс"}


def _format_entity_section(entity: EntityDecayResult) -> str:
    kind_label = _KIND_LABELS.get(entity.entity_kind, entity.entity_kind)
    b, r = entity.baseline, entity.recent
    sortino_b = f"{b.sortino:.2f}" if b.sortino is not None else "n/a"
    sortino_r = f"{r.sortino:.2f}" if r.sortino is not None else "n/a"
    wilson_cp = entity.wilson_changepoint
    sortino_cp = entity.sortino_changepoint

    lines = [
        f"🔻 <b>{entity.entity_name}</b> ({kind_label})",
        f"  Wilson: baseline {b.wilson_lower:.1%}-{b.wilson_upper:.1%} "
        f"→ recent {r.wilson_lower:.1%}-{r.wilson_upper:.1%}",
        f"  Sortino: baseline {sortino_b} → recent {sortino_r}",
        f"  CUSUM(Wilson): S={wilson_cp.cusum_statistic:.1f} (порог {wilson_cp.threshold:.1f}), "
        f"triggered={wilson_cp.triggered}, direction={wilson_cp.direction}",
        f"  CUSUM(Sortino): S={sortino_cp.cusum_statistic:.1f} (порог {sortino_cp.threshold:.1f}), "
        f"triggered={sortino_cp.triggered}, direction={sortino_cp.direction}",
        f"  n: baseline={b.n}, recent={r.n}",
    ]
    return "\n".join(lines)


def format_alert(entities: list[EntityDecayResult]) -> str:
    """Один текст на весь daily-прогон. Секция на каждую decaying-сущность.

    Без decaying-сущностей → "" (app/jobs/edge_decay_monitor.py не шлёт TG).
    """
    decaying = [e for e in entities if is_decaying(e)]
    if not decaying:
        return ""

    header = "⚠️ <b>Edge Decay Alert</b>\n\nОбнаружен changepoint (CUSUM) в следующих сущностях:\n"
    sections = "\n\n".join(_format_entity_section(e) for e in decaying)
    return f"{header}\n{sections}"


def _explain(entity: EntityDecayResult) -> list[str]:
    """Цифры с расшифровкой, что они значат. Сырые CUSUM-статистики сюда НЕ идут.

    Точность печатается до ДЕСЯТЫХ: округление до целых схлопывало 90.0% и 90.2%
    в одинаковые «90%», и главный вывод выглядел как «ничего не изменилось».

    Расхождение метрик (точность держится/растёт, а доходность падает) называется
    вслух — это ровно тот вопрос, с которого началась переделка сообщений: владелец
    видел рост Wilson и падение Sortino рядом и не понимал, что это значит вместе.
    """
    b, r = entity.baseline, entity.recent
    sortino_fell = b.sortino is not None and r.sortino is not None and r.sortino < b.sortino
    accuracy_held = r.wilson_lower >= b.wilson_lower

    lines = [f"Точность предсказаний: было {b.wilson_lower:.1%}, стало {r.wilson_lower:.1%} верных."]
    if sortino_fell:
        # round(v, 2) + 0 убирает «-0.00»: у отрицательного околонулевого форматирование
        # оставляет минус, который читается как опечатка. Просто `+0` не помогает —
        # знак теряется только после округления до той же точности, что и вывод.
        lines.append(
            f"Доходность с поправкой на риск просела: было {round(b.sortino, 2) + 0:.2f}, "
            f"стало {round(r.sortino, 2) + 0:.2f}. "
            "Проще говоря: редкие проигрыши стали дороже и съедают прибыль."
        )
        if accuracy_held:
            lines.append(
                "Метрики расходятся: угадывает не реже прежнего, но денег это приносит меньше — "
                "значит дело не в точности, а в цене ошибок."
            )
    lines.append(f"Считано по {r.n} сделкам за недавний период против {b.n} в базовом.")
    return lines


def _evidence(entity: EntityDecayResult) -> list[str]:
    """Сырые метрики — справочно, последней строкой."""
    w, s = entity.wilson_changepoint, entity.sortino_changepoint
    return [
        f"CUSUM(Wilson) S={w.cusum_statistic:.1f}/порог {w.threshold:.1f}",
        f"CUSUM(Sortino) S={s.cusum_statistic:.1f}/порог {s.threshold:.1f}",
        f"n: базовый={entity.baseline.n}, недавний={entity.recent.n}",
    ]


def to_notifications(entities: list[EntityDecayResult]) -> list[Notification]:
    """EntityDecayResult → Notification (спека §3).

    Денежного прогноза здесь нет намеренно: edge decay ловится на детекторах
    в тени/virtual, где реальные деньги не теряются. Выдумывать сумму запрещено
    (спека §3) — вместо неё качественное последствие.
    """
    out = []
    for e in entities:
        if not is_decaying(e):
            continue
        out.append(
            Notification(
                severity=Severity.ACTION_REQUIRED,
                subject=f"{e.entity_kind}:{e.entity_name}",
                kind="edge_decay",
                headline="Преимущество детектора тает",
                what_happened=_explain(e),
                what_if_ignored=(
                    "детектор не в live — реальных денег не теряешь. "
                    "Но если продвинуть его в таком виде, убыток станет настоящим"
                ),
                # НЕ обещать карточку: кнопка 🔍 пишет claim с пустым evidence_run_ids,
                # а improvement_cards_monitor берёт только claim'ы с непустым — карточки
                # от этой кнопки не появится (см. _handle_advisory_callback, INVESTIGATE).
                what_to_do="Деньги не горят. Нажми 🔍 — отмечу к разбору, разбирать причину руками.",
                actions=[],
                evidence=_evidence(e),
            )
        )
    return out
