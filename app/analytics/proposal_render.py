"""Рендер advisory-сообщений policy engine (Phase 2 C3a + человеческий текст 2026-08-01).

Детерминированный: Proposal → HTML-текст для Telegram по классу вердикта
(TG-спека §4.2 🟠 advisory / §4.6 🟡 DQ / §4.7 🟡).

Формат (спека 2026-08-01-advisory-actionable-text-design.md §3): заголовок → «Кто» →
объяснение с цифрами → «Если ничего не делать» → «Что делать» → подсказка по кнопкам →
основание. Цель: сообщение понятно без чтения кода и заканчивается конкретным шагом.

LLM здесь НЕТ и не будет (§2 п.2): шаблоны детерминированные. Ежедневная LLM-генерация
означала бы автоматическую отправку прод-P&L наружу без клика владельца.
"""

from __future__ import annotations

import html
import re

from app.analytics.policy_engine import (
    VERDICT_CLOSE_POSITION_REVIEW,
    VERDICT_INVESTIGATE_DATA,
    VERDICT_KEEP_SHADOW,
    VERDICT_PAUSE_REVIEW,
    VERDICT_REDUCE_RISK_REVIEW,
    Proposal,
)

# Классы 🟠 advisory (§4.2) — шлются только при policy_engine_enabled.
_ORANGE_VERDICTS = frozenset({VERDICT_PAUSE_REVIEW, VERDICT_REDUCE_RISK_REVIEW, VERDICT_CLOSE_POSITION_REVIEW})
# Классы 🟡 (§4.6/§4.7) — KEEP_SHADOW и INVESTIGATE_DATA.
_YELLOW_VERDICTS = frozenset({VERDICT_INVESTIGATE_DATA, VERDICT_KEEP_SHADOW})

# Заголовок несёт суть, а не название вердикта (§4 спеки).
_HEADINGS = {
    VERDICT_INVESTIGATE_DATA: "Данным нельзя доверять",
    VERDICT_KEEP_SHADOW: "Держим в тени, в live не выпускаем",
    VERDICT_PAUSE_REVIEW: "Преимущество тает — предлагаю паузу",
    VERDICT_REDUCE_RISK_REVIEW: "Предлагаю снизить риск",
    VERDICT_CLOSE_POSITION_REVIEW: "Детектор теряет деньги — предлагаю остановить",
}

# Конкретный следующий шаг под каждый вердикт (§4).
_ACTIONS = {
    VERDICT_CLOSE_POSITION_REVIEW: (
        "🚑 <b>Что делать:</b> останови торговлю по этому детектору вручную "
        "(дашборд → детектор → paused). Потом нажми ✅ — запишу решение "
        "и через 30 дней сам проверю, помогло ли."
    ),
    VERDICT_REDUCE_RISK_REVIEW: (
        "⚖️ <b>Что делать:</b> урежь размер ставок по этому детектору примерно вдвое. "
        "Нажми ✅ после того, как сделаешь — запишу и проверю результат через 30 дней."
    ),
    # 🔍 не заводит Improvement Card: claim пишется с пустым evidence_run_ids,
    # improvement_cards_monitor такие пропускает. Обещать карточку нельзя.
    VERDICT_PAUSE_REVIEW: (
        "⏸ <b>Что делать:</b> детектор не в live, деньги не горят — но продвигать его "
        "сейчас нельзя. Нажми 🔍, чтобы отметить к разбору."
    ),
    VERDICT_KEEP_SHADOW: (
        "🛡 <b>Что делать:</b> ничего. Это страховка: детектор не пойдёт в live, пока проблема не уйдёт."
    ),
    VERDICT_INVESTIGATE_DATA: (
        "🔍 <b>Что делать:</b> проверь источник данных, прежде чем принимать решения "
        "по деньгам. Пока данные под вопросом, риск-советов не даю."
    ),
}

# Последствие бездействия там, где денежной цифры НЕТ (§2 п.3 — не выдумывать).
_QUALITATIVE_COST = {
    VERDICT_PAUSE_REVIEW: (
        "⏳ <b>Если ничего не делать:</b> детектор останется кандидатом на promote — "
        "и в live уедет уже сломанное преимущество, уже реальными деньгами."
    ),
    VERDICT_INVESTIGATE_DATA: (
        "⏳ <b>Если ничего не делать:</b> риск-советы не выдаются, пока данные под "
        "вопросом — агент молчит вместо того, чтобы советовать по мусору."
    ),
}

_BUTTON_HINT = "<i>✅ = «сделал» · 🔍 = отметить к разбору · 📋 = подробный разбор</i>"
# Кнопки L1 есть только у 🟠-классов (C3b): у 🟡 подсказка была бы враньём.
_VERDICTS_WITH_BUTTONS = _ORANGE_VERDICTS

_DQ_MARKERS = {"GREEN": "🟢", "AMBER": "🟡", "RED": "🔴"}
_DQ_WORDS = {"GREEN": "в порядке", "AMBER": "под вопросом", "RED": "ненадёжны"}

# Человеческие имена детекторов. Неизвестный → печатается сырой id (fail-open, §2 п.5).
# Фронтендовый DETECTOR_LABELS намеренно не переиспользуется: неполон и сам жаргонен.
_DETECTOR_NAMES = {
    "D3_cross_mkt_arb": "D3 — арбитраж между рынками",
    "D13_retail_confidence_fade": "D13 — фейд розничной уверенности",
    "D14_crypto_winners_distribute_no": "D14 — распределение крипто-победителей",
    "D26_fade_whale_extreme": "D26 — фейд экстремумов китов",
    "D29_negrisk_basket_arb": "D29 — арбитраж negrisk-корзин",
    "D36_obi_pressure": "D36 — давление стакана",
    "D38_resolution_lag": "D38 — задержка резолва",
    "D40_kalshi_cross_platform": "D40 — межплощадочный Kalshi",
    "D42_event_driven_ml": "D42 — событийная ML-модель",
    "D44_late_convergence_carry": "D44 — поздняя сходимость",
    "D45b_lead_lag_cross_venue": "D45b — опережение между площадками",
    "D49_strike_ladder": "D49 — лестница страйков",
    "M2_liquidity_vacuum": "M2 — вакуум ликвидности",
    "STANDALONE_V2_LGB": "STANDALONE_V2_LGB — основная ML-модель",
    "d45_chainlink_leadlag": "D45 — опережение Chainlink",
}

_STAGE_WORDS = {
    "live": "живая торговля",
    "virtual": "виртуальная торговля",
    "shadow": "тень",
    "paused": "на паузе",
}

# Парсинг reason — fail-open (§2 п.6): не совпало → печатается исходный текст.
_DRAWDOWN_RE = re.compile(
    r"live drawdown: net ROI (?P<roi>-?[\d.]+)% over (?P<days>\d+)d "
    r"\(net \$(?P<net>-?[\d.]+) / stake \$(?P<stake>[\d.]+), n=(?P<n>\d+)\)"
)
_DECAY_RE = re.compile(r"edge decay \(stage=(?P<stage>\w+)\)")
_ADVERSE_RE = re.compile(r"adverse selection in (?P<cat>.+?) \((?P<mode>[^)]+)\)")
_DQ_RE = re.compile(r"DQ audit verdict (?P<verdict>\w+)(?: \((?P<detail>.+)\))?")
# Формат C0 после PR #738: «DQ RED — сработали проверки:\n• check: VERDICT — detail».
_DQ_CHECKS_RE = re.compile(r"DQ (?P<verdict>\w+) — сработали проверки:\n(?P<lines>.+)", re.DOTALL)
# Имя проверки САМО содержит ':' (freshness:detector_signals) — жадность до последнего
# ': VERDICT — ', иначе [^:]+ обрывается на первом двоеточии и строка не матчится.
_DQ_LINE_RE = re.compile(r"^• (?P<check>.+?): (?P<verdict>GREEN|AMBER|RED) — (?P<detail>.+)$")

# Имена проверок C0 → человеческие. Неизвестная → печатается как есть (fail-open).
_CHECK_NAMES = {
    "freshness:markets": "рынки давно не обновлялись",
    "freshness:detector_signals": "сигналы детекторов давно не обновлялись",
    "completeness:end_date": "у рынков не хватает даты закрытия",
    "completeness:tokens": "у рынков не хватает токенов",
    "expired_share": "слишком много резолвов пришло как EXPIRED",
    "ri:orphans": "сигналы ссылаются на несуществующие рынки",
}


def _detector_name(scope: str) -> str:
    """scope → человеческое имя. 'detector:D3:category:Crypto' → 'D3 — ... (Crypto)'."""
    if scope.startswith("policy:"):
        return "Качество данных"
    rest = scope.removeprefix("detector:")
    detector, _, category = rest.partition(":category:")
    name = _DETECTOR_NAMES.get(detector, detector)
    return f"{name} (категория: {category})" if category else name


def _humanize_reason(reason: str) -> str:
    """Машинный reason → русское объяснение с цифрами. Fail-open: не распознал → как есть."""
    m = _DRAWDOWN_RE.search(reason)
    if m:
        net = abs(float(m["net"]))
        return (
            f"За последние {m['days']} дней детектор сделал {m['n']} сделок: "
            f"вложено ${m['stake']}, потеряно ${net:.2f}.\n"
            f"Это <b>{m['roi']}%</b> — убыток стабильный, а не разовое невезение."
        )
    m = _DECAY_RE.search(reason)
    if m:
        stage = _STAGE_WORDS.get(m["stage"], m["stage"])
        return (
            "Точность предсказаний просела относительно того, что детектор показывал раньше.\n"
            f"Сейчас он в режиме «{html.escape(stage)}»."
        )
    m = _ADVERSE_RE.search(reason)
    if m:
        return (
            f"В категории «{html.escape(m['cat'].strip())}» детектор раз за разом заходит "
            "против тех, кто знает больше.\nПроще говоря: его переигрывают именно там."
        )
    m = _DQ_CHECKS_RE.search(reason)
    if m:
        out = ["Проверка качества данных нашла проблемы:"]
        for raw in m["lines"].splitlines():
            line = _DQ_LINE_RE.match(raw.strip())
            if line is None:
                out.append(html.escape(raw.strip()))  # fail-open: строка неизвестного вида
                continue
            check = line["check"].strip()
            name = _CHECK_NAMES.get(check, check)
            out.append(f"• {html.escape(name)} — {html.escape(line['detail'].strip())}")
        return "\n".join(out)
    m = _DQ_RE.search(reason)
    if m:
        base = "Проверка качества данных дала тревожный результат."
        detail = m["detail"]
        return f"{base}\nСработало: {html.escape(detail)}" if detail else base
    return html.escape(reason)


def _money_cost(reason: str) -> str | None:
    """«Если ничего не делать» с честной цифрой — только из фактических net/stake (§2 п.3).

    Экстраполяция линейная и названа как оценка: темп убытка × тот же горизонт.
    Возвращает None, если чисел в reason нет — выдумывать цифру запрещено.
    """
    m = _DRAWDOWN_RE.search(reason)
    if not m:
        return None
    net = abs(float(m["net"]))
    days = int(m["days"])
    if days <= 0:
        return None
    per_day = net / days
    return (
        f"💸 <b>Если ничего не делать:</b> при том же темпе это ещё примерно "
        f"<b>${net:.0f}</b> за следующие {days} дней (${per_day:.2f} в день)."
    )


def _evidence_line(proposal: Proposal, dq_verdict: str, ips_version: str | None) -> str:
    runs = " ".join(f"#{r}" for r in proposal.evidence_run_ids) or "—"
    marker = _DQ_MARKERS.get(dq_verdict, "⚪")
    word = _DQ_WORDS.get(dq_verdict, dq_verdict)
    parts = [f"run {runs}", f"данные {marker} {word}", f"правила v{proposal.rule_version}"]
    if ips_version is not None:
        parts.append(f"IPS {ips_version[:8]}")
    return "<i>основание: " + " · ".join(parts) + "</i>"


def _visible_emoji(proposal: Proposal) -> str | None:
    """Классификация видимости: 🟠 advisory / 🟡 DQ-подобный / None (blocked, OBSERVE и т.п.)."""
    if proposal.blocked:
        return None
    if proposal.verdict in _ORANGE_VERDICTS:
        return "🟠"
    if proposal.verdict in _YELLOW_VERDICTS:
        return "🟡"
    return None  # OBSERVE / START_VIRTUAL_REVIEW / LIVE_RAMP_REVIEW — не шлются


def render_proposal(proposal: Proposal, dq_verdict: str, ips_version: str | None) -> str | None:
    """Proposal → HTML advisory-текст, или None если класс не шлётся (OBSERVE/blocked)."""
    emoji = _visible_emoji(proposal)
    if emoji is None:
        return None

    heading = _HEADINGS.get(proposal.verdict, proposal.verdict)
    blocks: list[str] = [
        f"{emoji} <b>{heading}</b>\n<b>Кто:</b> {html.escape(_detector_name(proposal.scope))}",
        _humanize_reason(proposal.reason),
    ]

    cost = _money_cost(proposal.reason) or _QUALITATIVE_COST.get(proposal.verdict)
    if cost:
        blocks.append(cost)

    action = _ACTIONS.get(proposal.verdict)
    if action:
        blocks.append(action)

    if proposal.verdict in _VERDICTS_WITH_BUTTONS:
        blocks.append(_BUTTON_HINT)

    blocks.append(_evidence_line(proposal, dq_verdict, ips_version))
    return "\n\n".join(blocks)


def should_send(proposal: Proposal, enabled: bool) -> bool:
    """Подлежит ли отправке. 🟡 DQ (INVESTIGATE_DATA) — всегда; остальное — только enabled (спека §5)."""
    if _visible_emoji(proposal) is None:
        return False  # blocked / OBSERVE и прочие невидимые классы
    if proposal.verdict == VERDICT_INVESTIGATE_DATA:
        return True  # 🟡 DQ уходит даже в shadow
    return enabled
