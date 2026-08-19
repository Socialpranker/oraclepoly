"""Golden-реплеи Phase 2 C5, уровень А — чистый слой evaluate() + render_proposal().

Спека: docs/superpowers/specs/2026-07-29-phase2-c5-golden-harness-design.md §5.
Три независимых слоя проверок на сценарий — намеренно НЕ сливаются в один assert:
вердикты / обязательные слоты / полнотекст.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.analytics.policy_engine import evaluate
from app.analytics.proposal_render import render_proposal
from tests.golden.scenarios import ALL_SCENARIOS, GoldenScenario, build_dq_status

_MESSAGES_DIR = Path(__file__).parent / "golden" / "messages"

_IDS = [s.key for s in ALL_SCENARIOS]


def _run(scenario: GoldenScenario):
    return evaluate(
        build_dq_status(scenario),
        list(scenario.signals),
        list(scenario.drawdowns),
        scenario.thresholds,
        scenario.rule_version,
    )


def _rendered(scenario: GoldenScenario) -> str:
    """Все видимые сообщения сценария, склеенные по стабильному ключу сортировки."""
    dq_verdict = build_dq_status(scenario).verdict
    texts = []
    for proposal in sorted(_run(scenario), key=lambda p: (p.verdict, p.scope)):
        text = render_proposal(proposal, dq_verdict, scenario.ips_version)
        if text is not None:
            texts.append(text)
    return "\n\n---\n\n".join(texts)


@pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=_IDS)
def test_golden_verdicts(scenario: GoldenScenario):
    """Слой 1: множество (verdict, scope) совпадает с ожидаемым.

    Сравнение множеством, не списком: порядок зависит от входа, а незасеенный
    флейк в CI уже был (bootstrap seed, PR #694).
    """
    actual = {(p.verdict, p.scope) for p in _run(scenario)}
    assert actual == set(scenario.expected), (
        f"[{scenario.key}] вердикты разошлись с golden-ожиданием.\n"
        f"источник данных: {scenario.source}\n"
        f"ожидалось: {sorted(set(scenario.expected))}\n"
        f"получено:  {sorted(actual)}"
    )


@pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=_IDS)
def test_golden_required_slots(scenario: GoldenScenario):
    """Слой 2: причинные элементы (§6 родительской спеки) присутствуют в тексте.

    Живёт отдельно от полнотекста намеренно: даже если снапшот перепишут
    не глядя, потеря цифры или scope уронит этот тест.
    """
    text = _rendered(scenario)
    missing = [slot for slot in scenario.required_slots if slot not in text]
    assert not missing, (
        f"[{scenario.key}] в сообщении нет обязательных причинных элементов: {missing}\n"
        f"текст:\n{text}"
    )


@pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=_IDS)
def test_golden_blocked_risk_verdicts(scenario: GoldenScenario):
    """DQ RED обязан блокировать risk-вердикты (спека §4 родителя).

    Заблокированный вердикт не исчезает — он эмитится с blocked=True и
    blocked_by='DQ_RED', а render_proposal возвращает для него None
    (живёт как BLOCKED_DATA-клейм, не как сообщение).
    """
    proposals = _run(scenario)
    blocked = {p.scope for p in proposals if p.blocked}
    assert blocked == set(scenario.expected_blocked), (
        f"[{scenario.key}] блокировка risk-вердиктов разошлась с ожиданием.\n"
        f"ожидалось: {sorted(set(scenario.expected_blocked))}\n"
        f"получено:  {sorted(blocked)}"
    )
    for proposal in proposals:
        if proposal.blocked:
            assert proposal.blocked_by == "DQ_RED", (
                f"[{scenario.key}] {proposal.scope}: blocked_by={proposal.blocked_by}, ожидался DQ_RED"
            )


@pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=_IDS)
def test_golden_message_snapshot(scenario: GoldenScenario):
    """Слой 3: полнотекст равен снапшоту.

    Снапшоты правятся РУКАМИ (спека §2 п.5) — флага --update-golden нет намеренно.
    """
    snapshot_path = _MESSAGES_DIR / f"{scenario.key}.txt"
    assert snapshot_path.exists(), (
        f"[{scenario.key}] нет снапшота {snapshot_path}. "
        f"Создай его вручную, вставив фактический рендер и ПРОЧИТАВ его глазами."
    )
    expected = snapshot_path.read_text(encoding="utf-8")
    assert _rendered(scenario) == expected, (
        f"[{scenario.key}] сообщение разошлось со снапшотом {snapshot_path}.\n"
        f"Если изменение намеренное — обнови файл руками и перечитай текст: "
        f"его увидит владелец в Telegram."
    )


def test_thresholds_match_code_defaults():
    """Пороги фикстур обязаны совпадать с дефолтами кода (спека §2 п.4).

    Смена порогов — легитимное действие, но она обязана быть ОСОЗНАННОЙ:
    надо пересмотреть ожидаемые вердикты и перечитать 5 сообщений.
    Этот тест не даёт протащить смену молча мимо golden-ожиданий.

    ЧЕГО ТЕСТ НЕ ЛОВИТ (спека §6): дрейф боевых порогов в БД (policy_rules)
    против дефолтов кода. Сверка идёт только с _DEFAULT_PARAMS.
    """
    from app.analytics.policy_engine import _DEFAULT_PARAMS, PolicyThresholds
    from tests.golden.scenarios import THRESHOLDS_V1

    assert PolicyThresholds.from_params(_DEFAULT_PARAMS) == THRESHOLDS_V1, (
        "пороги policy engine изменились относительно замороженных в golden-фикстурах.\n"
        f"код:     {PolicyThresholds.from_params(_DEFAULT_PARAMS)}\n"
        f"фикстуры: {THRESHOLDS_V1}\n"
        "Если смена намеренная: обнови THRESHOLDS_V1, пересмотри expected у всех "
        "сценариев и ПЕРЕЧИТАЙ 5 снапшотов в tests/golden/messages/ — их увидит владелец."
    )


def test_all_scenarios_present():
    """§6 родительской спеки требует 5 инцидентов-реплеев; 06 добавлен сверх них
    для покрытия правила adverse_selection (гэп §6 спеки C5, закрыт 2026-07-30)."""
    assert len(ALL_SCENARIOS) == 6, f"ожидалось 6 сценариев, есть {len(ALL_SCENARIOS)}"
    assert len({s.key for s in ALL_SCENARIOS}) == 6, "ключи сценариев не уникальны"


def test_every_policy_rule_has_a_scenario():
    """Каждое правило §4 родительской спеки обязано быть покрыто хотя бы одним
    сценарием — иначе его можно сломать при зелёном harness'е.

    Именно так был найден гэп adverse_selection (мутационный тест 2026-07-29):
    5 сценариев замораживали 5 из 6 правил, о чём CI молчал. Тест проверяет
    вход (какие правила сценарии вообще способны активировать), а не выход —
    вердикты уже проверены test_golden_verdicts.
    """
    monitors = {s.monitor for scenario in ALL_SCENARIOS for s in scenario.signals}
    assert monitors == {"edge_decay", "adverse_selection"}, (
        f"не все мониторы-входы §4 представлены в сценариях: {sorted(monitors)}"
    )
    # adverse_selection ветвится по stage — нужны обе стороны (policy_engine.py:147-167).
    adverse_stages = {
        s.stage for scenario in ALL_SCENARIOS for s in scenario.signals if s.monitor == "adverse_selection"
    }
    assert "live" in adverse_stages and adverse_stages - {"live"}, (
        f"adverse_selection покрыт только частично: stage'и {sorted(adverse_stages, key=str)}. "
        f"Нужны и live (REDUCE_RISK_REVIEW), и не-live (KEEP_SHADOW, блок promote)."
    )
    assert any(scenario.drawdowns for scenario in ALL_SCENARIOS), "правило live drawdown не покрыто"


def test_every_scenario_declares_source():
    """Спека §2 п.3: происхождение чисел обязано быть явным.

    Проверяется не только префикс, но и содержимое после него: пустой хвост
    ('reconstructed:   ') объявлял бы происхождение формально, оставаясь
    бессмысленным. Для прод-слепка требуется разбираемая дата — без неё
    нельзя понять, к какому состоянию прода относятся числа.
    """
    for scenario in ALL_SCENARIOS:
        source = scenario.source
        assert source.startswith(("prod-query ", "reconstructed: ")), (
            f"[{scenario.key}] поле source='{source}' не в формате "
            f"'prod-query YYYY-MM-DD' или 'reconstructed: <постмортем>'"
        )
        prefix, _, tail = source.partition(" " if source.startswith("prod-query") else ": ")
        assert tail.strip(), (
            f"[{scenario.key}] source='{source}': после префикса '{prefix}' пусто — "
            f"происхождение чисел объявлено формально, но не указано"
        )
        if source.startswith("prod-query "):
            try:
                date.fromisoformat(tail.strip())
            except ValueError:
                raise AssertionError(
                    f"[{scenario.key}] source='{source}': за 'prod-query ' ожидалась дата "
                    f"YYYY-MM-DD (когда снят слепок прода), получено '{tail.strip()}'"
                ) from None


def test_expired_spike_fixture_has_enough_resolved():
    """Страховка от вырождения сценария 03 (спека §4, ловушка).

    classify_expired_share при resolved_total < expired_min_resolved вернёт
    GREEN независимо от доли EXPIRED. Если кто-то уменьшит n в фикстуре,
    сценарий тихо перестанет быть RED-инцидентом — этот тест не даст.
    """
    from tests.golden.scenarios import SCENARIO_03_EXPIRED_SPIKE

    check = SCENARIO_03_EXPIRED_SPIKE.dq_checks[0]
    assert check.verdict == "RED", (
        f"сценарий 03 выродился: expired_share = {check.verdict}, ожидался RED. "
        f"Причина обычно в resolved_total < expired_min_resolved (порог 50). "
        f"detail: {check.detail}"
    )
    assert check.measured == 100.0, f"ожидалась доля 100%, получено {check.measured}"
