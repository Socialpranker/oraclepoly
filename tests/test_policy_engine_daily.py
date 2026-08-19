"""Тесты джобы policy engine (Phase 2 C1+C2). Мок-паттерн: tests/test_dq_audit_daily.py."""

import json
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest

from app.analytics.ips import INACTIVE_IPS_GATE, IpsGate
from app.analytics.policy_engine import (
    _DEFAULT_PARAMS,
    VERDICT_CLOSE_POSITION_REVIEW,
    VERDICT_INVESTIGATE_DATA,
    VERDICT_OBSERVE,
    VERDICT_PAUSE_REVIEW,
    VERDICT_REDUCE_RISK_REVIEW,
    DqCheckLine,
    DqStatus,
    LiveDrawdown,
    PolicyThresholds,
    Proposal,
    proposal_id,
)
from app.jobs.policy_engine_daily import (
    _fetch_dq_status,
    _fetch_live_drawdowns,
    _fetch_monitor_signals,
    _load_ips_gate,
    _load_rule,
    _persist_blocked_claims,
    _persist_proposals,
    _send_advisory,
    run_policy_engine_daily,
)

_VALID_IPS = """
capital_usd: 1000
max_drawdown_pct: -0.20
liquidity_need_usd: 200
weekly_time_budget_hours: 5
forbidden_categories: politics
forbidden_platforms: none
"""


@pytest.mark.asyncio
async def test_load_rule_falls_back_to_defaults_when_missing():
    with patch("app.jobs.policy_engine_daily.fetchrow", new=AsyncMock(return_value=None)):
        version, thresholds = await _load_rule()
    assert version == 0
    assert thresholds == PolicyThresholds.from_params(_DEFAULT_PARAMS)


@pytest.mark.asyncio
async def test_load_rule_survives_missing_table():
    err = asyncpg.UndefinedTableError('relation "policy_rules" does not exist')
    with patch("app.jobs.policy_engine_daily.fetchrow", new=AsyncMock(side_effect=err)):
        version, thresholds = await _load_rule()
    assert version == 0


@pytest.mark.asyncio
async def test_fetch_dq_status_fail_closed_when_no_run():
    with patch("app.jobs.policy_engine_daily.fetchrow", new=AsyncMock(return_value=None)):
        dq = await _fetch_dq_status()
    assert dq == DqStatus(verdict="RED", run_id=None)


@pytest.mark.asyncio
async def test_fetch_dq_status_reads_latest_run():
    row = {"id": 166, "verdict": "GREEN"}
    with patch("app.jobs.policy_engine_daily.fetchrow", new=AsyncMock(return_value=row)):
        dq = await _fetch_dq_status()
    assert dq == DqStatus(verdict="GREEN", run_id=166)


_DQ_METRICS = {
    "rule_version": 1,
    "lookback_days": 7,
    "checks": [
        {
            "check": "freshness:markets",
            "verdict": "RED",
            "measured": 2713.0,
            "amber": 120.0,
            "red": 720.0,
            "detail": "markets: возраст последней записи 2713 мин",
        },
        {
            "check": "expired_share",
            "verdict": "GREEN",
            "measured": 22.75,
            "amber": 30.0,
            "red": 60.0,
            "detail": "EXPIRED 1710/7516 = 22.8%",
        },
    ],
}


@pytest.mark.asyncio
async def test_fetch_dq_status_parses_checks_from_dict_metrics():
    row = {"id": 257, "verdict": "RED", "metrics": _DQ_METRICS}
    with patch("app.jobs.policy_engine_daily.fetchrow", new=AsyncMock(return_value=row)):
        dq = await _fetch_dq_status()
    assert dq.run_id == 257
    # dict- и str-формы metrics обязаны давать идентичный результат — та же полная
    # проверка кортежа, что и в test_fetch_dq_status_parses_checks_from_str_metrics.
    assert dq.checks == (
        DqCheckLine("freshness:markets", "RED", "markets: возраст последней записи 2713 мин"),
        DqCheckLine("expired_share", "GREEN", "EXPIRED 1710/7516 = 22.8%"),
    )


@pytest.mark.asyncio
async def test_fetch_dq_status_parses_checks_from_str_metrics():
    # asyncpg отдаёт jsonb то как dict, то как str — обе формы обязаны работать
    # одинаково (спека §10 п.1; инцидент asyncpg-uuid-mock-blindspot-2026-07-21).
    row = {"id": 257, "verdict": "RED", "metrics": json.dumps(_DQ_METRICS)}
    with patch("app.jobs.policy_engine_daily.fetchrow", new=AsyncMock(return_value=row)):
        dq = await _fetch_dq_status()
    assert dq.checks == (
        DqCheckLine("freshness:markets", "RED", "markets: возраст последней записи 2713 мин"),
        DqCheckLine("expired_share", "GREEN", "EXPIRED 1710/7516 = 22.8%"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metrics",
    [None, {}, {"checks": []}, {"checks": "не-список"}, "{битый json", {"rule_version": 1}],
)
async def test_fetch_dq_status_tolerates_unusable_metrics(metrics):
    row = {"id": 257, "verdict": "RED", "metrics": metrics}
    with patch("app.jobs.policy_engine_daily.fetchrow", new=AsyncMock(return_value=row)):
        dq = await _fetch_dq_status()
    assert dq == DqStatus(verdict="RED", run_id=257, checks=())


@pytest.mark.asyncio
async def test_fetch_dq_status_skips_malformed_check_entries():
    # Контракт §6 мог поломаться на стороне C0 — пропускаем кривые записи, не падаем.
    row = {
        "id": 257,
        "verdict": "RED",
        "metrics": {
            "checks": [
                {"detail": "без ключа check"},
                "строка вместо объекта",
                {"check": "ri:orphans", "verdict": "RED", "detail": "сирот 5/100 = 5.00%"},
            ]
        },
    }
    with patch("app.jobs.policy_engine_daily.fetchrow", new=AsyncMock(return_value=row)):
        dq = await _fetch_dq_status()
    assert dq.checks == (DqCheckLine("ri:orphans", "RED", "сирот 5/100 = 5.00%"),)


@pytest.mark.asyncio
async def test_fetch_dq_status_row_without_metrics_key():
    # Страховка от KeyError: старый мок/строка без колонки metrics.
    with patch("app.jobs.policy_engine_daily.fetchrow", new=AsyncMock(return_value={"id": 166, "verdict": "GREEN"})):
        dq = await _fetch_dq_status()
    assert dq == DqStatus(verdict="GREEN", run_id=166, checks=())


@pytest.mark.asyncio
async def test_fetch_monitor_signals_parses_names_and_joins_stage():
    now = datetime.now(UTC)
    rows = [
        {
            "id": 7,
            "name": "edge_decay_monitor:detector:D38_resolution_lag",
            "verdict": "decaying",
            "started_at": now,
        },
        {
            "id": 10,
            "name": "adverse_selection_monitor:D36_obi_pressure×Sports",
            "verdict": "adverse",
            "started_at": now,
        },
        {"id": 11, "name": "adverse_selection_monitor:run_manifest", "verdict": "manifest", "started_at": now},  # skip
        {
            "id": 12,
            "name": "edge_decay_monitor:methodology:exit_strategy",
            "verdict": "stable",
            "started_at": now,
        },  # skip: не detector
    ]
    stages = {"D38_resolution_lag": "virtual", "D36_obi_pressure": "paused"}
    with patch("app.jobs.policy_engine_daily.fetch", new=AsyncMock(return_value=rows)):
        signals = await _fetch_monitor_signals(stages)
    assert len(signals) == 2
    decay = next(s for s in signals if s.monitor == "edge_decay")
    assert (decay.entity, decay.stage, decay.category) == ("D38_resolution_lag", "virtual", None)
    adv = next(s for s in signals if s.monitor == "adverse_selection")
    assert (adv.entity, adv.category, adv.stage) == ("D36_obi_pressure", "Sports", "paused")


@pytest.mark.asyncio
async def test_fetch_monitor_signals_warns_on_stale_run(caplog):
    stale_row = {
        "id": 20,
        "name": "edge_decay_monitor:detector:D3_cross_mkt_arb",
        "verdict": "stable",
        "started_at": datetime.now(UTC) - timedelta(hours=10),
    }
    with (
        patch("app.jobs.policy_engine_daily.fetch", new=AsyncMock(return_value=[stale_row])),
        caplog.at_level("WARNING"),
    ):
        await _fetch_monitor_signals({})
    assert "policy_engine_stale_monitor_run" in caplog.text
    assert "D3_cross_mkt_arb" in caplog.text


@pytest.mark.asyncio
async def test_fetch_monitor_signals_no_warning_when_fresh(caplog):
    fresh_row = {
        "id": 21,
        "name": "edge_decay_monitor:detector:D3_cross_mkt_arb",
        "verdict": "stable",
        "started_at": datetime.now(UTC) - timedelta(hours=1),
    }
    with (
        patch("app.jobs.policy_engine_daily.fetch", new=AsyncMock(return_value=[fresh_row])),
        caplog.at_level("WARNING"),
    ):
        await _fetch_monitor_signals({})
    assert "policy_engine_stale_monitor_run" not in caplog.text


@pytest.mark.asyncio
async def test_fetch_live_drawdowns_maps_rows():
    rows = [{"detector": "D3_cross_mkt_arb", "n_settled": 39, "net_pnl": -80.80, "stake": 242.26}]
    with patch("app.jobs.policy_engine_daily.fetch", new=AsyncMock(return_value=rows)):
        dds = await _fetch_live_drawdowns(window_days=30)
    assert dds[0].detector == "D3_cross_mkt_arb"
    assert dds[0].n_settled == 39


_OBSERVE = Proposal(verdict=VERDICT_OBSERVE, scope="portfolio:all", reason="all rules clean", rule_version=1)
_PAUSE = Proposal(
    verdict=VERDICT_PAUSE_REVIEW,
    scope="detector:D38_resolution_lag",
    reason="edge decay (stage=virtual)",
    evidence_run_ids=(7,),
    rule_version=1,
)
_BLOCKED = Proposal(
    verdict=VERDICT_CLOSE_POSITION_REVIEW,
    scope="detector:D3_cross_mkt_arb",
    reason="live drawdown",
    rule_version=1,
    blocked=True,
)


@pytest.mark.asyncio
async def test_persist_proposals_writes_and_dedups():
    # первый scope — не в cooldown (fetchrow → None), затем L2-снапшот того же proposal
    # (fetchrow → None, detector_state пуст), второй scope — в cooldown (fetchrow → строка)
    fetchrow_mock = AsyncMock(side_effect=[None, None, {"?column?": 1}])
    exec_mock = AsyncMock(return_value="INSERT 0 1")
    with (
        patch("app.jobs.policy_engine_daily.fetchrow", new=fetchrow_mock),
        patch("app.jobs.policy_engine_daily.execute", new=exec_mock),
    ):
        written, skipped = await _persist_proposals(
            [_OBSERVE, _PAUSE], cooldown_hours=6, ips_version=None, enabled=False
        )
    assert (len(written), skipped) == (1, 1)
    exec_mock.assert_awaited_once()
    args = exec_mock.await_args.args
    assert "policy_engine:portfolio:all" in args  # name
    assert VERDICT_OBSERVE in args  # verdict


@pytest.mark.asyncio
async def test_persist_proposals_cooldown_is_time_based_not_windowed_hash():
    """Регресс 2026-08-01: cooldown искал config_hash, а тот содержит календарный день.
    На суточной джобе хэш менялся каждый прогон → cooldown не срабатывал никогда
    (6 одинаковых advisory про D3 за 9 дней). Ключ должен быть стабильным (name+verdict),
    а окно времени — в SQL-предикате started_at."""
    fetchrow_mock = AsyncMock(return_value=None)
    with (
        patch("app.jobs.policy_engine_daily.fetchrow", new=fetchrow_mock),
        patch("app.jobs.policy_engine_daily.execute", new=AsyncMock(return_value="INSERT 0 1")),
    ):
        await _persist_proposals([_OBSERVE], cooldown_hours=6, ips_version=None, enabled=False)

    # await_args — ПОСЛЕДНИЙ вызов; с Phase 3 fetchrow зовётся ещё и для L2-снапшота,
    # поэтому cooldown-вызов берём явно первым.
    sql, *params = fetchrow_mock.await_args_list[0].args
    # календарного окна в ключе cooldown быть не должно ни в каком виде
    assert "config_hash" not in sql
    window = datetime.now(UTC).strftime("%Y-%m-%d")
    assert not any(window in str(p) for p in params)
    # ключ — стабильные name+verdict, окно времени — отдельным предикатом
    assert params[0] == "policy_engine:portfolio:all"
    assert params[1] == VERDICT_OBSERVE
    assert params[2] == "6"
    assert "started_at > now()" in sql


@pytest.mark.asyncio
async def test_persist_proposals_cooldown_does_not_swallow_verdict_escalation():
    """Cooldown по одному лишь scope глушил бы эскалацию вердикта для того же детектора.
    Смена PAUSE_REVIEW → CLOSE_POSITION_REVIEW — новое событие, должна доходить сразу."""
    seen: list[tuple] = []

    async def fake_fetchrow(_sql, *params):
        # Phase 3: fetchrow зовётся и для L2-снапшота (1 параметр — detector).
        # Cooldown-вызов узнаём по трём параметрам (name, verdict, hours).
        if len(params) < 3:
            return {"stage": "live", "live_stake_cap_usd": None}
        seen.append(params)
        # в cooldown только PAUSE_REVIEW по этому scope; эскалация — не в cooldown
        return {"?column?": 1} if params[1] == VERDICT_PAUSE_REVIEW else None

    escalated = Proposal(
        verdict=VERDICT_CLOSE_POSITION_REVIEW,
        scope=_PAUSE.scope,
        reason="live drawdown deepened",
        rule_version=1,
    )
    with (
        patch("app.jobs.policy_engine_daily.fetchrow", new=fake_fetchrow),
        patch("app.jobs.policy_engine_daily.execute", new=AsyncMock(return_value="INSERT 0 1")),
    ):
        written, skipped = await _persist_proposals(
            [_PAUSE, escalated], cooldown_hours=6, ips_version=None, enabled=False
        )

    assert (len(written), skipped) == (1, 1)
    assert written[0][0].verdict == VERDICT_CLOSE_POSITION_REVIEW
    # оба запроса — по одному и тому же scope, различает их только verdict
    assert {p[0] for p in seen} == {f"policy_engine:{_PAUSE.scope}"}


@pytest.mark.asyncio
async def test_persist_proposals_skips_blocked():
    exec_mock = AsyncMock(return_value="INSERT 0 1")
    with (
        patch("app.jobs.policy_engine_daily.fetchrow", new=AsyncMock(return_value=None)),
        patch("app.jobs.policy_engine_daily.execute", new=exec_mock),
    ):
        written, skipped = await _persist_proposals([_BLOCKED], cooldown_hours=6, ips_version=None, enabled=False)
    assert (len(written), skipped) == (0, 0)
    exec_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_persist_proposals_on_conflict_race_counts_as_skipped():
    """Cooldown-SELECT говорит "не в cooldown" (fetchrow→None), но параллельный прогон уже
    успел вставить тот же config_hash — ON CONFLICT DO NOTHING → 'INSERT 0 0' → skipped,
    не written (иначе written-счётчик врёт о реальной гонке)."""
    exec_mock = AsyncMock(return_value="INSERT 0 0")
    with (
        patch("app.jobs.policy_engine_daily.fetchrow", new=AsyncMock(return_value=None)),
        patch("app.jobs.policy_engine_daily.execute", new=exec_mock),
    ):
        written, skipped = await _persist_proposals([_OBSERVE], cooldown_hours=6, ips_version=None, enabled=False)
    assert (len(written), skipped) == (0, 1)
    exec_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_persist_blocked_claims_upserts_per_blocked_proposal():
    exec_mock = AsyncMock()
    with patch("app.jobs.policy_engine_daily.execute", new=exec_mock):
        n = await _persist_blocked_claims([_BLOCKED, _PAUSE], dq_run_id=102)
    assert n == 1  # только blocked=True
    args = exec_mock.await_args.args
    assert args[1] == "policy verdict blocked by DQ_RED: CLOSE_POSITION_REVIEW detector:D3_cross_mkt_arb"
    assert args[2] == "detector:D3_cross_mkt_arb"


@pytest.mark.asyncio
async def test_blocked_claims_statement_names_ips_violation():
    p = Proposal(
        verdict=VERDICT_CLOSE_POSITION_REVIEW,
        scope="detector:D3",
        reason="x",
        rule_version=1,
        blocked=True,
        blocked_by="IPS_VIOLATION",
    )
    captured = {}

    async def fake_execute(sql, *args):
        captured["statement"] = args[0]
        return "INSERT 0 1"

    with patch("app.jobs.policy_engine_daily.execute", new=fake_execute):
        n = await _persist_blocked_claims([p], dq_run_id=None)
    assert n == 1
    assert captured["statement"] == "policy verdict blocked by IPS_VIOLATION: CLOSE_POSITION_REVIEW detector:D3"


class _FakeSettings:
    policy_engine_enabled = False
    admin_telegram_ids = [111]


def _patched_run(dq, signals, drawdowns):
    return (
        patch("app.jobs.policy_engine_daily.get_settings", new=lambda: _FakeSettings()),
        patch(
            "app.jobs.policy_engine_daily._load_rule",
            new=AsyncMock(return_value=(1, PolicyThresholds.from_params(_DEFAULT_PARAMS))),
        ),
        patch("app.jobs.policy_engine_daily._fetch_dq_status", new=AsyncMock(return_value=dq)),
        patch("app.jobs.policy_engine_daily._fetch_detector_stages", new=AsyncMock(return_value={})),
        patch("app.jobs.policy_engine_daily._fetch_monitor_signals", new=AsyncMock(return_value=signals)),
        patch("app.jobs.policy_engine_daily._fetch_live_drawdowns", new=AsyncMock(return_value=drawdowns)),
        patch("app.jobs.policy_engine_daily._load_ips_gate", new=AsyncMock(return_value=INACTIVE_IPS_GATE)),
        patch(
            "app.jobs.policy_engine_daily._persist_proposals",
            new=AsyncMock(return_value=([(_OBSERVE, "pid_observe_00000", False)], 0)),
        ),
        patch("app.jobs.policy_engine_daily._persist_blocked_claims", new=AsyncMock(return_value=0)),
        patch("app.jobs.policy_engine_daily._send_advisory", new=AsyncMock(return_value=(0, 0))),
    )


@pytest.mark.asyncio
async def test_run_clean_cycle_summary():
    with ExitStack() as stack:
        for p in _patched_run(DqStatus("GREEN", 100), [], []):
            stack.enter_context(p)
        summary = await run_policy_engine_daily()
    assert summary["dq_verdict"] == "GREEN"
    assert summary["proposals_written"] == 1
    assert summary["blocked_claims"] == 0
    assert summary["risk_blocked"] is False
    assert summary["rule_version"] == 1


@pytest.mark.asyncio
async def test_run_dq_red_marks_cycle_blocked():
    with ExitStack() as stack:
        patches = list(_patched_run(DqStatus("RED", 102), [], []))
        stack.enter_context(patches[0])  # get_settings
        stack.enter_context(patches[1])  # _load_rule
        stack.enter_context(patches[2])  # _fetch_dq_status
        stack.enter_context(patches[3])  # _fetch_detector_stages
        stack.enter_context(patches[4])  # _fetch_monitor_signals
        stack.enter_context(patches[5])  # _fetch_live_drawdowns
        ips_gate_mock = AsyncMock(return_value=INACTIVE_IPS_GATE)
        stack.enter_context(patch("app.jobs.policy_engine_daily._load_ips_gate", new=ips_gate_mock))
        stack.enter_context(patches[7])  # _persist_proposals
        stack.enter_context(patches[8])  # _persist_blocked_claims
        stack.enter_context(patches[9])  # _send_advisory
        summary = await run_policy_engine_daily()
    assert summary["risk_blocked"] is True
    assert summary["dq_verdict"] == "RED"
    # whole-branch ревью C3a fix: реальный dq.verdict должен доходить до _load_ips_gate,
    # чтобы IPS-parse-🟡-маркер (если файл не парсится) нёс правдивый DQ-статус, не хардкод.
    ips_gate_mock.assert_awaited_once()
    assert ips_gate_mock.await_args.args[-1] == "RED"


@pytest.mark.asyncio
async def test_run_dq_red_with_blocked_risks_writes_observe_blocked_marker():
    """Спека §5: DQ RED + подавленные risk-вердикты → отдельный OBSERVE-run-маркер
    (config_hash в неймспейсе 'OBSERVE_BLOCKED', metrics.suppressed_count=n, metrics.blocked=True)."""
    exec_mock = AsyncMock(return_value="INSERT 0 1")
    with ExitStack() as stack:
        patches = list(_patched_run(DqStatus("RED", 102), [], []))
        # Заменяем _persist_blocked_claims на ненулевой возврат — маркер пишется только при n>0
        patches[-2] = patch("app.jobs.policy_engine_daily._persist_blocked_claims", new=AsyncMock(return_value=2))
        for p in patches:
            stack.enter_context(p)
        stack.enter_context(patch("app.jobs.policy_engine_daily.execute", new=exec_mock))
        summary = await run_policy_engine_daily()

    assert summary["risk_blocked"] is True
    exec_mock.assert_awaited_once()
    args = exec_mock.await_args.args  # args[0] = SQL; params start at [1]
    assert args[1] == "policy_engine:portfolio:all"  # name
    assert args[-1] == VERDICT_OBSERVE  # verdict
    metrics = json.loads(args[5])
    assert metrics["blocked"] is True
    assert metrics["suppressed_count"] == 2
    assert "cycle blocked by DQ RED" in metrics["reason"]
    # отдельный неймспейс хэша — не совпадает с обычным OBSERVE того же окна/версии
    window = datetime.now(UTC).strftime("%Y-%m-%d")
    expected_pid = proposal_id("OBSERVE_BLOCKED", "portfolio:all", 1, window)
    normal_observe_pid = proposal_id(VERDICT_OBSERVE, "portfolio:all", 1, window)
    assert args[2] == expected_pid
    assert expected_pid != normal_observe_pid


@pytest.mark.asyncio
async def test_run_dq_green_never_writes_blocked_marker():
    exec_mock = AsyncMock(return_value="INSERT 0 1")
    with ExitStack() as stack:
        for p in _patched_run(DqStatus("GREEN", 100), [], []):
            stack.enter_context(p)
        stack.enter_context(patch("app.jobs.policy_engine_daily.execute", new=exec_mock))
        await run_policy_engine_daily()
    exec_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_ips_violated_blocks_risk_and_writes_marker():
    """C2: DQ GREEN + IPS violated → risk-вердикты (drawdown) блокируются как при DQ RED,
    плюс INVESTIGATE_DATA-proposal 'policy:ips' (не blocked) + OBSERVE_BLOCKED-маркер."""
    ips_gate = IpsGate(
        active=True,
        violated=True,
        violations=("forbidden category exposure: politics ($12.50)",),
        version_hash="deadbeef00000000",
    )
    drawdowns = [LiveDrawdown(detector="D3", n_settled=30, net_pnl=-40.0, stake=100.0)]
    exec_calls: list[tuple] = []
    sent_texts: list[str] = []

    async def fake_execute(sql, *args):
        exec_calls.append(args)
        return "INSERT 0 1"

    async def fake_rows(chat_id, text, rows, parse_mode="HTML"):
        sent_texts.append(text)
        return True

    with (
        patch("app.jobs.policy_engine_daily.get_settings", new=lambda: _FakeSettings()),
        patch(
            "app.jobs.policy_engine_daily._load_rule",
            new=AsyncMock(return_value=(1, PolicyThresholds.from_params(_DEFAULT_PARAMS))),
        ),
        patch("app.jobs.policy_engine_daily._fetch_dq_status", new=AsyncMock(return_value=DqStatus("GREEN", 166))),
        patch("app.jobs.policy_engine_daily._fetch_detector_stages", new=AsyncMock(return_value={})),
        patch("app.jobs.policy_engine_daily._fetch_monitor_signals", new=AsyncMock(return_value=[])),
        patch("app.jobs.policy_engine_daily._fetch_live_drawdowns", new=AsyncMock(return_value=drawdowns)),
        patch("app.jobs.policy_engine_daily._load_ips_gate", new=AsyncMock(return_value=ips_gate)),
        patch("app.jobs.policy_engine_daily.fetchrow", new=AsyncMock(return_value=None)),
        patch("app.jobs.policy_engine_daily.execute", new=fake_execute),
        patch("app.api.telegram.send_telegram_with_keyboard_rows", fake_rows),
    ):
        summary = await run_policy_engine_daily()

    assert summary["risk_blocked"] is True
    assert summary["ips_violated"] is True
    assert summary["ips_version"] == "deadbeef00000000"
    assert summary["blocked_claims"] == 1
    # IPS-INVESTIGATE_DATA (policy:ips) не blocked → written → уходит даже в shadow
    assert summary["advisory_sent"] >= 1
    assert summary["advisory_send_failures"] == 0
    assert summary["policy_engine_enabled"] is False
    assert sent_texts  # advisory реально ушёл (через send_telegram_with_keyboard_rows)

    blocked_claim_call = next(
        c
        for c in exec_calls
        if c and c[0] == "policy verdict blocked by IPS_VIOLATION: CLOSE_POSITION_REVIEW detector:D3"
    )
    assert blocked_claim_call[1] == "detector:D3"

    observe_marker_call = next(c for c in exec_calls if c[-1] == VERDICT_OBSERVE)
    marker_metrics = json.loads(observe_marker_call[4])
    assert "IPS_VIOLATION" in marker_metrics["reason"]
    assert marker_metrics["ips_version"] == "deadbeef00000000"

    investigate_call = next(c for c in exec_calls if c[0] == "policy_engine:policy:ips")
    assert investigate_call[-1] == VERDICT_INVESTIGATE_DATA
    investigate_metrics = json.loads(investigate_call[4])
    assert investigate_metrics.get("blocked", False) is False


@pytest.mark.asyncio
async def test_run_shadow_disabled_silences_advisory_send():
    """Shadow-инвариант C1 (обновлено C3b Task 2): в shadow (policy_engine_enabled=False)
    advisory-🟠 не уходит — should_send гейтит по enabled, telegram-вызов реален лишь для
    видимых (🟡 DQ) proposals. С C3b джоба уже импортирует app.api.telegram — старая
    строковая проверка "telegram не импортируется вовсе" устарела содержательно."""
    with (
        patch("app.jobs.policy_engine_daily.get_settings", new=lambda: _FakeSettings()),
        patch(
            "app.jobs.policy_engine_daily._load_rule",
            new=AsyncMock(return_value=(1, PolicyThresholds.from_params(_DEFAULT_PARAMS))),
        ),
        patch("app.jobs.policy_engine_daily._fetch_dq_status", new=AsyncMock(return_value=DqStatus("GREEN", 100))),
        patch("app.jobs.policy_engine_daily._fetch_detector_stages", new=AsyncMock(return_value={})),
        patch("app.jobs.policy_engine_daily._fetch_monitor_signals", new=AsyncMock(return_value=[])),
        patch("app.jobs.policy_engine_daily._fetch_live_drawdowns", new=AsyncMock(return_value=[])),
        patch("app.jobs.policy_engine_daily._load_ips_gate", new=AsyncMock(return_value=INACTIVE_IPS_GATE)),
        patch(
            "app.jobs.policy_engine_daily._persist_proposals",
            new=AsyncMock(return_value=([(_PAUSE, "pid_pause_000000", False)], 0)),
        ),
        patch("app.jobs.policy_engine_daily._persist_blocked_claims", new=AsyncMock(return_value=0)),
    ):
        summary = await run_policy_engine_daily()
    # _FakeSettings.policy_engine_enabled == False → advisory 🟠 (PAUSE_REVIEW) молчит
    assert summary["advisory_sent"] == 0
    assert summary["advisory_send_failures"] == 0


@pytest.mark.asyncio
async def test_load_ips_gate_absent_file_inactive(tmp_path: Path):
    with patch("app.jobs.policy_engine_daily._IPS_PATH", tmp_path / "nope.md"):
        gate = await _load_ips_gate(drawdowns=[], window_days=30)
    assert gate == INACTIVE_IPS_GATE


@pytest.mark.asyncio
async def test_load_ips_gate_parse_error_inactive_and_marker(tmp_path: Path):
    bad = tmp_path / "owner_ips.local.md"
    bad.write_text("garbage no colon\n", encoding="utf-8")
    cooldown_miss = AsyncMock(return_value=None)  # cooldown-SELECT: маркера нет
    insert_ok = AsyncMock(return_value="INSERT 0 1")
    with (
        patch("app.jobs.policy_engine_daily._IPS_PATH", bad),
        patch("app.jobs.policy_engine_daily.fetchrow", new=cooldown_miss),
        patch("app.jobs.policy_engine_daily.execute", new=insert_ok),
    ):
        gate = await _load_ips_gate(drawdowns=[], window_days=30)
    assert gate == INACTIVE_IPS_GATE
    insert_ok.assert_awaited_once()  # 🟡-маркер записан
    verdict_arg = insert_ok.await_args.args[-1]
    assert verdict_arg == "INVESTIGATE_DATA"


@pytest.mark.asyncio
async def test_load_ips_gate_parse_error_marker_respects_cooldown(tmp_path: Path):
    bad = tmp_path / "owner_ips.local.md"
    bad.write_text("garbage\n", encoding="utf-8")
    cooldown_hit = AsyncMock(return_value={"?column?": 1})  # маркер уже был за 7д
    insert_spy = AsyncMock(return_value="INSERT 0 1")
    with (
        patch("app.jobs.policy_engine_daily._IPS_PATH", bad),
        patch("app.jobs.policy_engine_daily.fetchrow", new=cooldown_hit),
        patch("app.jobs.policy_engine_daily.execute", new=insert_spy),
    ):
        gate = await _load_ips_gate(drawdowns=[], window_days=30)
    assert gate == INACTIVE_IPS_GATE
    insert_spy.assert_not_awaited()  # однократно: cooldown 7д
    # cooldown time-based по name (не по оконному ISO-week pid) — переживает границу недели
    cooldown_call = cooldown_hit.await_args
    assert "name = 'policy_engine:policy:ips_parse'" in cooldown_call.args[0]
    assert cooldown_call.args[1] == "168"


@pytest.mark.asyncio
async def test_ips_parse_marker_sends_yellow_when_written(tmp_path: Path):
    """Ревью C3a: маркер реально вставлен (не в cooldown) → advisory уходит в TG (спека §5)."""
    bad = tmp_path / "owner_ips.local.md"
    bad.write_text("garbage no colon\n", encoding="utf-8")
    cooldown_miss = AsyncMock(return_value=None)  # cooldown-SELECT: маркера нет
    insert_ok = AsyncMock(return_value="INSERT 0 1")
    sent_texts: list[str] = []

    async def fake_send(text):
        sent_texts.append(text)

    with (
        patch("app.jobs.policy_engine_daily._IPS_PATH", bad),
        patch("app.jobs.policy_engine_daily.fetchrow", new=cooldown_miss),
        patch("app.jobs.policy_engine_daily.execute", new=insert_ok),
        patch("app.tasks.notifications.send_admin_message", new=fake_send),
    ):
        gate = await _load_ips_gate(drawdowns=[], window_days=30)

    assert gate == INACTIVE_IPS_GATE
    insert_ok.assert_awaited_once()
    assert sent_texts, "advisory должен уйти при реально вставленном маркере"
    assert sent_texts[0].startswith("🟡")


@pytest.mark.asyncio
async def test_ips_parse_marker_uses_real_dq_verdict_in_evidence_line(tmp_path: Path):
    """Whole-branch ревью C3a fix: evidence-строка должна нести РЕАЛЬНЫЙ DQ-вердикт цикла,
    не хардкод "GREEN" — раньше маркер всегда рисовал "DQ 🟢", даже когда DQ был RED/AMBER."""
    bad = tmp_path / "owner_ips.local.md"
    bad.write_text("garbage no colon\n", encoding="utf-8")
    cooldown_miss = AsyncMock(return_value=None)
    insert_ok = AsyncMock(return_value="INSERT 0 1")
    sent_texts: list[str] = []

    async def fake_send(text):
        sent_texts.append(text)

    with (
        patch("app.jobs.policy_engine_daily._IPS_PATH", bad),
        patch("app.jobs.policy_engine_daily.fetchrow", new=cooldown_miss),
        patch("app.jobs.policy_engine_daily.execute", new=insert_ok),
        patch("app.tasks.notifications.send_admin_message", new=fake_send),
    ):
        gate = await _load_ips_gate(drawdowns=[], window_days=30, dq_verdict="RED")

    assert gate == INACTIVE_IPS_GATE
    assert sent_texts
    assert "данные 🔴" in sent_texts[0]
    assert "DQ 🟢" not in sent_texts[0]


@pytest.mark.asyncio
async def test_ips_parse_marker_silent_when_cooldown(tmp_path: Path):
    """Ревью C3a: cooldown hit → ни INSERT, ни отправка (однократность 7д)."""
    bad = tmp_path / "owner_ips.local.md"
    bad.write_text("garbage\n", encoding="utf-8")
    cooldown_hit = AsyncMock(return_value={"?column?": 1})  # маркер уже был за 7д
    insert_spy = AsyncMock(return_value="INSERT 0 1")
    sent_texts: list[str] = []

    async def fake_send(text):
        sent_texts.append(text)

    with (
        patch("app.jobs.policy_engine_daily._IPS_PATH", bad),
        patch("app.jobs.policy_engine_daily.fetchrow", new=cooldown_hit),
        patch("app.jobs.policy_engine_daily.execute", new=insert_spy),
        patch("app.tasks.notifications.send_admin_message", new=fake_send),
    ):
        gate = await _load_ips_gate(drawdowns=[], window_days=30)

    assert gate == INACTIVE_IPS_GATE
    insert_spy.assert_not_awaited()
    assert sent_texts == []


@pytest.mark.asyncio
async def test_load_ips_gate_active_clean(tmp_path: Path):
    f = tmp_path / "owner_ips.local.md"
    f.write_text(_VALID_IPS, encoding="utf-8")
    exposure_rows: list = []  # нет live-экспозиции в forbidden
    with (
        patch("app.jobs.policy_engine_daily._IPS_PATH", f),
        patch("app.jobs.policy_engine_daily.fetch", new=AsyncMock(return_value=exposure_rows)),
    ):
        gate = await _load_ips_gate(drawdowns=[], window_days=30)
    assert gate.active and not gate.violated
    assert gate.version_hash is not None


@pytest.mark.asyncio
async def test_load_ips_gate_violated_by_portfolio_drawdown(tmp_path: Path):
    f = tmp_path / "owner_ips.local.md"
    f.write_text(_VALID_IPS, encoding="utf-8")
    dd = [
        LiveDrawdown(detector="D3", n_settled=30, net_pnl=-40.0, stake=100.0),
        LiveDrawdown(detector="D13", n_settled=10, net_pnl=5.0, stake=50.0),
    ]  # портфель: −35/150 = −23.3% < −20%
    with (
        patch("app.jobs.policy_engine_daily._IPS_PATH", f),
        patch("app.jobs.policy_engine_daily.fetch", new=AsyncMock(return_value=[])),
    ):
        gate = await _load_ips_gate(drawdowns=dd, window_days=30)
    assert gate.violated
    assert any("drawdown" in v for v in gate.violations)


@pytest.mark.asyncio
async def test_load_ips_gate_violated_by_forbidden_exposure(tmp_path: Path):
    f = tmp_path / "owner_ips.local.md"
    f.write_text(_VALID_IPS, encoding="utf-8")
    exposure_rows = [{"category": "Politics", "stake": 12.5}]
    with (
        patch("app.jobs.policy_engine_daily._IPS_PATH", f),
        patch("app.jobs.policy_engine_daily.fetch", new=AsyncMock(return_value=exposure_rows)),
    ):
        gate = await _load_ips_gate(drawdowns=[], window_days=30)
    assert gate.violated
    assert any("politics" in v for v in gate.violations)


@pytest.mark.asyncio
async def test_persist_proposals_carries_ips_version():
    p = Proposal(verdict=VERDICT_OBSERVE, scope="portfolio:all", reason="all rules clean", rule_version=1)
    captured = {}

    async def fake_execute(sql, *args):
        captured["metrics"] = json.loads(args[4])
        return "INSERT 0 1"

    with (
        patch("app.jobs.policy_engine_daily.fetchrow", new=AsyncMock(return_value=None)),
        patch("app.jobs.policy_engine_daily.execute", new=fake_execute),
    ):
        await _persist_proposals([p], cooldown_hours=6, ips_version="deadbeef00000000", enabled=False)
    assert captured["metrics"]["ips_version"] == "deadbeef00000000"


@pytest.mark.asyncio
async def test_persist_proposals_returns_written_list():
    p = Proposal(verdict=VERDICT_REDUCE_RISK_REVIEW, scope="detector:D3", reason="x", rule_version=1)
    with (
        patch("app.jobs.policy_engine_daily.fetchrow", new=AsyncMock(return_value=None)),
        patch("app.jobs.policy_engine_daily.execute", new=AsyncMock(return_value="INSERT 0 1")),
    ):
        written, skipped = await _persist_proposals([p], cooldown_hours=6, ips_version=None, enabled=False)
    assert len(written) == 1 and skipped == 0
    written_p, written_pid, has_l2 = written[0]
    assert written_p == p
    assert isinstance(written_pid, str) and written_pid
    assert has_l2 is False, "REDUCE_RISK без известного cap не создаёт L2-действие"


@pytest.mark.asyncio
async def test_persist_proposals_creates_l2_action_for_actionable_verdict():
    """PAUSE_REVIEW → в agent_actions появляется L2-строка, и флаг has_l2=True."""
    p = Proposal(verdict="PAUSE_REVIEW", scope="detector:D3_cross_mkt_arb", reason="edge decay", rule_version=2)
    calls = []

    async def fake_execute(sql, *args):
        calls.append(sql)
        return "INSERT 0 1"

    with (
        patch(
            "app.jobs.policy_engine_daily.fetchrow",
            new=AsyncMock(side_effect=[None, {"stage": "live", "live_stake_cap_usd": None}]),
        ),
        patch("app.jobs.policy_engine_daily.execute", new=fake_execute),
    ):
        written, skipped = await _persist_proposals([p], cooldown_hours=25, ips_version=None, enabled=True)

    assert len(written) == 1
    assert written[0][2] is True, "флаг has_l2 должен быть True для PAUSE_REVIEW"
    assert any("agent_actions" in c for c in calls), "L2-строка не создана"


@pytest.mark.asyncio
async def test_persist_metrics_shadow_reflects_enabled():
    p = Proposal(verdict=VERDICT_REDUCE_RISK_REVIEW, scope="detector:D3", reason="x", rule_version=1)
    captured = {}

    async def fake_execute(sql, *args):
        captured["metrics"] = json.loads(args[4])
        return "INSERT 0 1"

    with (
        patch("app.jobs.policy_engine_daily.fetchrow", new=AsyncMock(return_value=None)),
        patch("app.jobs.policy_engine_daily.execute", new=fake_execute),
    ):
        await _persist_proposals([p], cooldown_hours=6, ips_version=None, enabled=True)
    assert captured["metrics"]["shadow"] is False


class _OneAdminSettings:
    admin_telegram_ids = [111]


@pytest.mark.asyncio
async def test_send_advisory_shadow_silences_orange_but_sends_dq():
    dq_p = Proposal(
        verdict=VERDICT_INVESTIGATE_DATA, scope="policy:dq_audit", reason="DQ audit verdict AMBER", rule_version=1
    )
    risk_p = Proposal(verdict=VERDICT_REDUCE_RISK_REVIEW, scope="detector:D3", reason="drawdown", rule_version=1)
    sent_texts = []

    async def fake_rows(chat_id, text, rows, parse_mode="HTML"):
        sent_texts.append(text)
        return True

    with (
        patch("app.api.telegram.send_telegram_with_keyboard_rows", fake_rows),
        patch("app.jobs.policy_engine_daily.get_settings", lambda: _OneAdminSettings()),
    ):
        sent, failures = await _send_advisory(
            [(dq_p, "pid_dq_00000000", False), (risk_p, "pid_risk_0000000", False)],
            dq_verdict="AMBER",
            ips_version=None,
            enabled=False,
        )
    assert (sent, failures) == (1, 0)  # только DQ ушёл
    assert sent_texts and sent_texts[0].startswith("🟡")


@pytest.mark.asyncio
async def test_send_advisory_enabled_sends_all_visible():
    risk_p = Proposal(verdict=VERDICT_REDUCE_RISK_REVIEW, scope="detector:D3", reason="drawdown", rule_version=1)
    sent_texts = []

    async def fake_rows(chat_id, text, rows, parse_mode="HTML"):
        sent_texts.append(text)
        return True

    with (
        patch("app.api.telegram.send_telegram_with_keyboard_rows", fake_rows),
        patch("app.jobs.policy_engine_daily.get_settings", lambda: _OneAdminSettings()),
    ):
        sent, failures = await _send_advisory(
            [(risk_p, "pid_risk_0000000", False)], dq_verdict="GREEN", ips_version=None, enabled=True
        )
    assert (sent, failures) == (1, 0) and sent_texts[0].startswith("🟠")


@pytest.mark.asyncio
async def test_send_advisory_skips_observe():
    obs = Proposal(verdict=VERDICT_OBSERVE, scope="portfolio:all", reason="all clean", rule_version=1)
    calls = []

    async def fake_rows(chat_id, text, rows, parse_mode="HTML"):
        calls.append(text)
        return True

    with (
        patch("app.api.telegram.send_telegram_with_keyboard_rows", fake_rows),
        patch("app.jobs.policy_engine_daily.get_settings", lambda: _OneAdminSettings()),
    ):
        sent, failures = await _send_advisory(
            [(obs, "pid_obs_00000000", False)], dq_verdict="GREEN", ips_version=None, enabled=True
        )
    assert (sent, failures) == (0, 0) and calls == []


@pytest.mark.asyncio
async def test_send_advisory_attaches_keyboard_and_counts_failures():
    sent_calls = []

    async def fake_rows(chat_id, text, rows, parse_mode="HTML"):
        sent_calls.append((chat_id, rows))
        return chat_id != 999  # 999 симулирует 5xx-фейл

    fake_settings = type("S", (), {"admin_telegram_ids": [111, 999]})()
    p = Proposal(verdict=VERDICT_REDUCE_RISK_REVIEW, scope="detector:D3", reason="roi -20%", rule_version=1)

    with (
        patch("app.api.telegram.send_telegram_with_keyboard_rows", fake_rows),
        patch("app.jobs.policy_engine_daily.get_settings", lambda: fake_settings),
    ):
        sent, failures = await _send_advisory([(p, "abc123def4567890", False)], "GREEN", None, enabled=True)

    assert sent == 1  # 111 доставлен
    assert failures == 1  # 999 упал
    assert sent_calls[0][1] == [
        [  # ряд из трёх кнопок
            {"text": "✅ Принял", "callback_data": "adv:ack:abc123def4567890"},
            {"text": "🔍 Investigate", "callback_data": "adv:investigate:abc123def4567890"},
            {"text": "📋 Request review", "callback_data": "adv:review:abc123def4567890"},
        ]
    ]


# ── Phase 3: создание L2-proposal вместе с advisory ──


def test_action_for_verdict_maps_pause():
    from app.analytics.policy_engine import Proposal
    from app.jobs.policy_engine_daily import _action_for_verdict

    p = Proposal(verdict="PAUSE_REVIEW", scope="detector:D3_cross_mkt_arb", reason="edge decay", rule_version=2)
    kind, payload = _action_for_verdict(p)
    assert kind == "pause_detector"
    assert payload["detector"] == "D3_cross_mkt_arb"


def test_action_for_verdict_maps_close_position_to_pause_not_close():
    """CLOSE_POSITION_REVIEW → пауза детектора, НЕ закрытие позиции.

    Вердикт говорит «останови детектор», а close_position требует конкретный
    order_db_id, которого у policy engine нет: он работает с агрегатом по детектору,
    а не с отдельными ордерами. Кнопка закрытия позиции живёт в другом контуре
    (close_pos:, Task 5).
    """
    from app.analytics.policy_engine import Proposal
    from app.jobs.policy_engine_daily import _action_for_verdict

    p = Proposal(
        verdict="CLOSE_POSITION_REVIEW", scope="detector:D3_cross_mkt_arb", reason="live drawdown", rule_version=2
    )
    kind, _ = _action_for_verdict(p)
    assert kind == "pause_detector"


def test_action_for_verdict_maps_reduce_cap_with_halved_value():
    from app.analytics.policy_engine import Proposal
    from app.jobs.policy_engine_daily import _action_for_verdict

    p = Proposal(verdict="REDUCE_RISK_REVIEW", scope="detector:D3_cross_mkt_arb", reason="adverse", rule_version=2)
    kind, payload = _action_for_verdict(p, current_cap=10.0)
    assert kind == "reduce_cap"
    assert payload["new_cap_usd"] == 5.0  # «урежь вдвое» — ровно то, что обещает текст кнопки


def test_action_for_verdict_returns_none_for_non_actionable():
    """KEEP_SHADOW/INVESTIGATE_DATA — действия нет, кнопки быть не должно."""
    from app.analytics.policy_engine import Proposal
    from app.jobs.policy_engine_daily import _action_for_verdict

    for verdict in ("KEEP_SHADOW", "INVESTIGATE_DATA", "OBSERVE"):
        p = Proposal(verdict=verdict, scope="policy:dq_audit", reason="x", rule_version=2)
        assert _action_for_verdict(p) is None


def test_action_for_verdict_returns_none_when_cap_unknown():
    """REDUCE_RISK без известного текущего cap — не выдумываем число."""
    from app.analytics.policy_engine import Proposal
    from app.jobs.policy_engine_daily import _action_for_verdict

    p = Proposal(verdict="REDUCE_RISK_REVIEW", scope="detector:D3", reason="x", rule_version=2)
    assert _action_for_verdict(p, current_cap=None) is None


@pytest.mark.asyncio
async def test_create_l2_action_writes_row_with_ttl_24h():
    from app.analytics.policy_engine import Proposal
    from app.jobs.policy_engine_daily import _create_l2_action

    p = Proposal(verdict="PAUSE_REVIEW", scope="detector:D3_cross_mkt_arb", reason="edge decay", rule_version=2)
    execute_mock = AsyncMock(return_value="INSERT 0 1")
    with patch("app.jobs.policy_engine_daily.execute", new=execute_mock):
        action_id = await _create_l2_action(p, "pid123", {"stage": "live"})

    assert action_id == "pid123"
    sql = execute_mock.await_args.args[0]
    assert "'L2'" in sql
    assert "'proposed'" in sql
    assert "24 hours" in sql  # TTL из спеки §4


@pytest.mark.asyncio
async def test_create_l2_action_skips_non_actionable_verdict():
    from app.analytics.policy_engine import Proposal
    from app.jobs.policy_engine_daily import _create_l2_action

    p = Proposal(verdict="KEEP_SHADOW", scope="detector:D36", reason="adverse", rule_version=2)
    execute_mock = AsyncMock()
    with patch("app.jobs.policy_engine_daily.execute", new=execute_mock):
        action_id = await _create_l2_action(p, "pid999", {"stage": "shadow"})

    assert action_id is None
    execute_mock.assert_not_awaited()
