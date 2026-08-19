"""Golden-реплеи Phase 2 C5, уровень Б — сборка входа джобой.

Спека: docs/superpowers/specs/2026-07-29-phase2-c5-golden-harness-design.md §5.
Уровень А (test_policy_golden_replay.py) проверяет ПРАВИЛА; здесь проверяется,
что джоба доводит строки БД до этих правил неискажёнными — два инцидента,
где баг был именно в сборе данных (ID mismatch, live drawdown).

Мок-паттерн репо: patch на модульные атрибуты fetch/fetchrow
(см. tests/test_policy_engine_daily.py:48-53).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.analytics.policy_engine import evaluate
from tests.golden.scenarios import (
    SCENARIO_02_ID_MISMATCH,
    SCENARIO_05_LIVE_DRAWDOWN,
    build_dq_status,
)


@pytest.mark.asyncio
async def test_job_builds_drawdown_matching_golden_05():
    """Строки execution_orders → LiveDrawdown → тот же вердикт, что уровень А.

    Числа — прод-слепок 2026-07-29 (см. source сценария 05).
    """
    from app.jobs.policy_engine_daily import _fetch_live_drawdowns

    rows = [
        {
            "detector": "D3_cross_mkt_arb",
            "n_settled": 39,
            "net_pnl": -80.7962,
            "stake": 242.2600,
        }
    ]
    fetch_mock = AsyncMock(return_value=rows)
    with patch("app.jobs.policy_engine_daily.fetch", new=fetch_mock):
        drawdowns = await _fetch_live_drawdowns(30)

    assert list(drawdowns) == list(SCENARIO_05_LIVE_DRAWDOWN.drawdowns), (
        "джоба собрала LiveDrawdown иначе, чем заморожено в golden-фикстуре"
    )

    # Мок отдаёт rows на любые аргументы, поэтому окно проверяется отдельно:
    # без этого рефакторинг на другое окно (или его потерю) прошёл бы мимо теста.
    window_arg = fetch_mock.await_args.args[1]
    assert window_arg == "30", (
        f"окно drawdown'а не доехало до SQL: передано {window_arg!r}, ожидалось '30'. "
        f"Окно задаёт горизонт правила (drawdown_window_days) — подмена меняет вердикт."
    )

    proposals = evaluate(
        build_dq_status(SCENARIO_05_LIVE_DRAWDOWN),
        [],
        list(drawdowns),
        SCENARIO_05_LIVE_DRAWDOWN.thresholds,
        SCENARIO_05_LIVE_DRAWDOWN.rule_version,
    )
    actual = {(p.verdict, p.scope) for p in proposals}
    assert actual == set(SCENARIO_05_LIVE_DRAWDOWN.expected)


@pytest.mark.asyncio
async def test_job_builds_monitor_signal_matching_golden_02():
    """agent_runs(kind='monitoring') → MonitorSignal с корректным stage.

    ВНИМАНИЕ к формату имени (проверено по _parse_monitor_name, строка 146):
    префикс edge_decay — 'edge_decay_monitor:detector:<entity>', с ДВУМЯ
    сегментами. Имя без сегмента 'detector:' вернёт None, сигнал молча
    отфильтруется, и тест упадёт на пустом списке.
    """
    from app.jobs.policy_engine_daily import _fetch_monitor_signals

    rows = [
        {
            "id": 3002,
            "name": "edge_decay_monitor:detector:M2_liquidity_vacuum",
            "verdict": "decaying",
            "started_at": None,
        }
    ]
    stages = {"M2_liquidity_vacuum": "shadow"}
    with patch("app.jobs.policy_engine_daily.fetch", new=AsyncMock(return_value=rows)):
        signals = await _fetch_monitor_signals(stages)

    assert list(signals) == list(SCENARIO_02_ID_MISMATCH.signals), (
        "джоба собрала MonitorSignal иначе, чем заморожено в golden-фикстуре"
    )
