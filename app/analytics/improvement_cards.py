"""Improvement Cards — чистый аналитический слой (без DB).

Собирает предложения улучшений из выходов аудитов Phase 1
(adverse_selection / edge_decay / counterfactual), ранжирует по EV/час
владельца. Design doc:
docs/superpowers/specs/2026-07-21-improvement-cards-design.md

Здесь — чистые функции (классификация источника, извлечение leak_usd из
metrics, EV/час, mechanism_ref, attention-переходы). SQL и side-effects —
в app/jobs/improvement_cards_monitor.py.
"""

from __future__ import annotations

from dataclasses import dataclass

# name-namespace каждого аудита-источника → source_kind.
_SOURCE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("adverse_selection_monitor", "adverse_selection"),
    ("edge_decay_monitor", "edge_decay"),
    ("counterfactual", "counterfactual"),
)


@dataclass(frozen=True)
class TierConfig:
    p_fix_success: float
    effort_hours: float


@dataclass(frozen=True)
class CardDraft:
    card_key: str
    source_kind: str
    scope: str
    title: str
    claim_id: int
    evidence_run_ids: list[int]
    mechanism_ref: str | None
    leak_usd: float | None
    p_fix_success: float
    effort_hours: float
    lifecycle_stage: str


def classify_source_kind(run_name: str) -> str | None:
    """source_kind по name-namespace evidence-run; None если не аудит-источник."""
    for prefix, kind in _SOURCE_PREFIXES:
        if run_name.startswith(prefix):
            return kind
    return None


def extract_leak_usd_from_metrics(source_kind: str, metrics: dict) -> float | None:
    """Размер утечки $ из metrics run. edge_decay → None (досчёт в джобе своим SQL).
    counterfactual: pnl_diff_usd (как есть, знак определяет пишущий), иначе fallback
    tpsl_pnl-hold_to_resolution_pnl — sign-gated: возвращаем только delta<0 (утечка),
    delta>=0 (as-is политика впереди hold) → None."""
    if source_kind == "adverse_selection":
        val = metrics.get("net_pnl")
        return float(val) if val is not None else None
    if source_kind == "counterfactual":
        val = metrics.get("pnl_diff_usd")
        if val is not None:
            return float(val)
        # Реальный прод-скрипт scripts/research/counterfactual_case1_hold_vs_tpsl.py
        # не пишет pnl_diff_usd — он пишет tpsl_pnl (as-is факт) и
        # hold_to_resolution_pnl (контрфактика). Fallback = "утечка текущей
        # exit-политики": tpsl_pnl - hold_to_resolution_pnl. Отрицательное значение
        # означает, что hold-to-resolution лучше факта (as-is TP/SL), т.е. деньги
        # теряются на текущей политике — знак совпадает с конвенцией adverse_selection
        # (отрицательный leak = утечка). Sign-gate (как _edge_decay_leak_or_none в
        # джобе): delta >= 0 значит as-is политика ВПЕРЕДИ hold — утечки нет, карточка
        # не должна её показывать → None. Прод-инцидент id=7: tpsl=3912.51,
        # hold=2573.15 → delta=+1339.36 без гейта карточка врала про "$1,339 утечки".
        tpsl_val = metrics.get("tpsl_pnl")
        hold_val = metrics.get("hold_to_resolution_pnl")
        if tpsl_val is not None and hold_val is not None:
            delta = float(tpsl_val) - float(hold_val)
            return delta if delta < 0 else None
        return None
    # edge_decay: метрики $ нет
    return None


# Маппинг source_kind × scope → запись базы знаний (§3.4 контракт цитирования).
_MECHANISM_REFS: dict[str, str] = {
    "adverse_selection": "docs/knowledge/market_economics.md#adverse-selection",
    "edge_decay": "docs/knowledge/methods_playbook.md#changepoint",
    "counterfactual": "docs/knowledge/market_economics.md#exit-cost",
}
_TEMPERATURE_REF = "docs/knowledge/behavioral_catalog.md#temperature-bots"

_ATTENTION_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"acted", "acknowledged", "ignored"},
    "acknowledged": {"acted", "ignored"},
    "acted": set(),
    "ignored": set(),
}


def resolve_mechanism_ref(source_kind: str, scope: str, is_temperature: bool) -> str | None:
    """Ссылка на запись базы знаний (§3.4 правило 2). Temperature → поведенческий каталог."""
    if is_temperature:
        return _TEMPERATURE_REF
    return _MECHANISM_REFS.get(source_kind)


def build_title(source_kind: str, scope: str, leak_usd: float | None) -> str:
    """Человекочитаемый заголовок карточки."""
    label = {
        "adverse_selection": "Adverse selection",
        "edge_decay": "Edge decay",
        "counterfactual": "Exit/policy",
    }.get(source_kind, source_kind)
    if leak_usd is not None:
        return f"{label}: {scope} теряет ${abs(leak_usd):,.0f}"
    return f"{label}: {scope} (утечка не измерена)"


def ev_per_hour(leak_usd: float | None, tier: TierConfig) -> float | None:
    """EV/час = |leak_usd| × p_fix / hours. None если leak_usd None или hours<=0.
    Зеркалит GENERATED-колонку improvement_cards.ev_per_hour."""
    if leak_usd is None or tier.effort_hours <= 0:
        return None
    return abs(leak_usd) * tier.p_fix_success / tier.effort_hours


def initial_lifecycle_stage(leak_usd: float | None) -> str:
    """Стартовый lifecycle-stage: с измеренной $-утечкой → digest, без → shadow."""
    return "digest" if leak_usd is not None else "shadow"


def next_attention_status(current: str, event: str) -> str:
    """Валидатор перехода attention_status. Вызовет Компонент 5 (кнопки).
    Невалидный переход → ValueError (защита от рассинхрона состояний)."""
    allowed = _ATTENTION_TRANSITIONS.get(current, set())
    if event not in allowed:
        raise ValueError(f"invalid attention transition {current!r} -> {event!r}")
    return event


def compute_action_rate(sent: int, acted: int) -> float | None:
    """action-rate типа выхода (§3.2). None если sent==0 (нет данных)."""
    if sent <= 0:
        return None
    return acted / sent
