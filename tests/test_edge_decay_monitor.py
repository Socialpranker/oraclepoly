"""Tests для app.jobs.edge_decay_monitor."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.analytics.changepoint import ChangepointResult
from app.analytics.edge_decay_alert_formatter import EntityDecayResult
from app.analytics.edge_metrics import WindowMetrics
from app.jobs.edge_decay_monitor import (
    EdgeDecayConfig,
    MonitoredEntity,
    OutcomeRow,
    _collect_detector_entities,
    _collect_methodology_entities,
    _fetch_outcome_rows,
    _load_config,
    _persist_claim_if_decaying,
    _persist_entity_run,
    _split_baseline_recent,
    run_edge_decay_monitor,
)
from app.services.notification_bus import PublishResult


@pytest.mark.asyncio
async def test_load_config_returns_row_values():
    row = {
        "baseline_days": 90,
        "recent_days": 14,
        "cusum_k": 0.5,
        "cusum_threshold": 5.0,
    }
    with patch("app.jobs.edge_decay_monitor.fetchrow", new=AsyncMock(return_value=row)):
        config = await _load_config()

    assert config == EdgeDecayConfig(baseline_days=90, recent_days=14, cusum_k=0.5, cusum_threshold=5.0)


@pytest.mark.asyncio
async def test_load_config_falls_back_to_defaults_when_table_empty():
    with patch("app.jobs.edge_decay_monitor.fetchrow", new=AsyncMock(return_value=None)):
        config = await _load_config()

    assert config == EdgeDecayConfig(baseline_days=90, recent_days=14, cusum_k=0.5, cusum_threshold=5.0)


@pytest.mark.asyncio
async def test_collect_detector_entities_delegates_to_ssot():
    """Список наблюдаемых берётся из fetch_active_detectors, не собирается заново."""
    with patch(
        "app.jobs.edge_decay_monitor.fetch_active_detectors",
        new=AsyncMock(return_value={"D3", "D38"}),
    ):
        entities = await _collect_detector_entities()

    assert entities == [
        MonitoredEntity(name="D3", kind="detector", detectors=["D3"]),
        MonitoredEntity(name="D38", kind="detector", detectors=["D38"]),
    ]


@pytest.mark.asyncio
async def test_collect_detector_entities_includes_wss_realtime_detectors():
    """WSS-realtime детектор (без cron-строки) попадает под надзор.

    Регресс 2026-08-04: здесь дублировалось голое `db_active & cron_active`, а
    whitelist WSS-детекторов применяла только fetch_active_detectors — которую
    никто не вызывал. Итог: M2 и подобные выпадали из edge_decay_monitor,
    потому что cron-строки у них нет by design (детекция в WSS-потоке).

    Проверяем сквозь настоящую fetch_active_detectors: детектор активен в БД,
    в cron его нет, но он в _WSS_REALTIME_DETECTORS — должен наблюдаться.
    """
    from app.services import detector_metrics

    wss_name = next(iter(detector_metrics._WSS_REALTIME_DETECTORS))
    db_rows = [{"detector": wss_name}, {"detector": "D_cron_only"}]

    with (
        patch.object(detector_metrics, "fetch", new=AsyncMock(return_value=db_rows)),
        patch.object(detector_metrics, "parse_active_cron_detectors", return_value={"D_cron_only"}),
    ):
        entities = await _collect_detector_entities()

    names = {e.name for e in entities}
    assert wss_name in names, f"{wss_name} (WSS-realtime) выпал из надзора — whitelist не применён"
    assert "D_cron_only" in names, "обычный cron-детектор тоже должен наблюдаться"


@pytest.mark.asyncio
async def test_collect_methodology_entities_parses_detectors_from_latest_run_metrics():
    scope_rows = [{"scope": "methodology:exit_policy"}]
    run_row = {"metrics": json.dumps({"detectors": ["D38", "D42"]})}

    async def _fetch(query, *args):
        if "DISTINCT scope" in query:
            return scope_rows
        return []

    with (
        patch("app.jobs.edge_decay_monitor.fetch", side_effect=_fetch),
        patch("app.jobs.edge_decay_monitor.fetchrow", new=AsyncMock(return_value=run_row)),
    ):
        entities = await _collect_methodology_entities()

    assert entities == [MonitoredEntity(name="methodology:exit_policy", kind="methodology", detectors=["D38", "D42"])]


@pytest.mark.asyncio
async def test_collect_methodology_entities_skips_claim_without_detectors_field():
    scope_rows = [{"scope": "methodology:no_data_source"}]

    async def _fetch(query, *args):
        if "DISTINCT scope" in query:
            return scope_rows
        return []

    with (
        patch("app.jobs.edge_decay_monitor.fetch", side_effect=_fetch),
        patch("app.jobs.edge_decay_monitor.fetchrow", new=AsyncMock(return_value=None)),
    ):
        entities = await _collect_methodology_entities()

    assert entities == []


@pytest.mark.asyncio
async def test_fetch_outcome_rows_uses_real_pnl_when_available():
    rows = [
        {
            "outcome": "WIN",
            "net_pnl": 12.5,
            "current_price": 0.3,
            "side": "YES",
            "generated_at": datetime(2026, 7, 1, tzinfo=UTC),
        }
    ]
    with patch("app.jobs.edge_decay_monitor.fetch", new=AsyncMock(return_value=rows)):
        result = await _fetch_outcome_rows(["D3"], datetime(2026, 6, 1, tzinfo=UTC))

    assert result == [OutcomeRow(outcome="WIN", pnl=12.5, generated_at=datetime(2026, 7, 1, tzinfo=UTC))]


@pytest.mark.asyncio
async def test_fetch_outcome_rows_falls_back_to_synthetic_pnl_when_no_execution():
    rows = [
        {
            "outcome": "WIN",
            "net_pnl": None,
            "current_price": 0.3,
            "side": "YES",
            "generated_at": datetime(2026, 7, 1, tzinfo=UTC),
        }
    ]
    with patch("app.jobs.edge_decay_monitor.fetch", new=AsyncMock(return_value=rows)):
        result = await _fetch_outcome_rows(["D3"], datetime(2026, 6, 1, tzinfo=UTC))

    assert result == [OutcomeRow(outcome="WIN", pnl=pytest.approx(0.7), generated_at=datetime(2026, 7, 1, tzinfo=UTC))]


def test_split_baseline_recent_partitions_by_cutoff():
    rows = [
        OutcomeRow(outcome="WIN", pnl=0.5, generated_at=datetime(2026, 6, 1, tzinfo=UTC)),
        OutcomeRow(outcome="LOSS", pnl=-0.3, generated_at=datetime(2026, 7, 15, tzinfo=UTC)),
    ]

    with patch("app.jobs.edge_decay_monitor._now", return_value=datetime(2026, 7, 21, tzinfo=UTC)):
        baseline, recent = _split_baseline_recent(rows, recent_days=14)

    assert baseline == [rows[0]]
    assert recent == [rows[1]]


_NOT_TRIGGERED = ChangepointResult(triggered=False, cusum_statistic=0.0, threshold=5.0, direction=None)
_TRIGGERED_BELOW = ChangepointResult(triggered=True, cusum_statistic=6.8, threshold=5.0, direction="below")


def _entity(name="D38", wilson_cp=_NOT_TRIGGERED, sortino_cp=_NOT_TRIGGERED) -> EntityDecayResult:
    metrics = WindowMetrics(n=10, wins=5, wilson_lower=0.3, wilson_upper=0.7, sortino=1.0)
    return EntityDecayResult(
        entity_name=name,
        entity_kind="detector",
        baseline=metrics,
        recent=metrics,
        wilson_changepoint=wilson_cp,
        sortino_changepoint=sortino_cp,
    )


@pytest.mark.asyncio
async def test_persist_entity_run_inserts_and_returns_id():
    with patch("app.jobs.edge_decay_monitor.fetch", new=AsyncMock(return_value=[{"id": 42}])):
        config = EdgeDecayConfig(baseline_days=90, recent_days=14, cusum_k=0.5, cusum_threshold=5.0)
        run_id = await _persist_entity_run(_entity(), config)

    assert run_id == 42


@pytest.mark.asyncio
async def test_persist_entity_run_serializes_infinite_sortino_as_valid_json():
    """Regression: json.dumps(float('inf')) emits the token `Infinity`, which
    Postgres's JSONB parser rejects (InvalidTextRepresentationError, found on
    prod dry-run 2026-07-21 for a detector with no downside in its recent
    window). The serialized metrics JSON must stay valid JSON — no bare
    Infinity token — while still surfacing the +inf signal as "inf"."""
    inf_metrics = WindowMetrics(n=18, wins=18, wilson_lower=0.82, wilson_upper=1.0, sortino=float("inf"))
    entity = EntityDecayResult(
        entity_name="D26",
        entity_kind="detector",
        baseline=inf_metrics,
        recent=inf_metrics,
        wilson_changepoint=_NOT_TRIGGERED,
        sortino_changepoint=_NOT_TRIGGERED,
    )
    fetch_mock = AsyncMock(return_value=[{"id": 7}])
    with patch("app.jobs.edge_decay_monitor.fetch", fetch_mock):
        config = EdgeDecayConfig(baseline_days=90, recent_days=14, cusum_k=0.5, cusum_threshold=5.0)
        await _persist_entity_run(entity, config)

    metrics_json_arg = fetch_mock.await_args.args[5]
    assert "Infinity" not in metrics_json_arg
    parsed = json.loads(metrics_json_arg)
    assert parsed["baseline"]["sortino"] == "inf"
    assert parsed["recent"]["sortino"] == "inf"


@pytest.mark.asyncio
async def test_persist_claim_if_decaying_writes_claim_when_decaying():
    execute_mock = AsyncMock(return_value="INSERT 0 1")
    with patch("app.jobs.edge_decay_monitor.execute", execute_mock):
        await _persist_claim_if_decaying(_entity(wilson_cp=_TRIGGERED_BELOW), run_id=42)

    execute_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_persist_claim_if_decaying_skips_when_not_decaying():
    execute_mock = AsyncMock(return_value="INSERT 0 1")
    with patch("app.jobs.edge_decay_monitor.execute", execute_mock):
        await _persist_claim_if_decaying(_entity(), run_id=42)

    execute_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_edge_decay_monitor_no_entities_returns_zero_counts():
    with (
        patch("app.jobs.edge_decay_monitor.fetchrow", new=AsyncMock(return_value=None)),
        patch("app.jobs.edge_decay_monitor.fetch_active_detectors", new=AsyncMock(return_value=set())),
        patch("app.jobs.edge_decay_monitor.fetch", new=AsyncMock(return_value=[])),
    ):
        result = await run_edge_decay_monitor()

    assert result == {"status": "ok", "n_entities": 0, "n_triggered": 0, "entities": []}


@pytest.mark.asyncio
async def test_run_edge_decay_monitor_sends_one_summary_alert_when_decaying():
    outcome_rows_baseline = [
        {
            "outcome": "WIN" if i % 2 == 0 else "LOSS",
            "net_pnl": 0.5 if i % 2 == 0 else -0.5,
            "current_price": 0.5,
            "side": "YES",
            "generated_at": datetime(2026, 5, 1, tzinfo=UTC),
        }
        for i in range(20)
    ]
    outcome_rows_recent = [
        {
            "outcome": "LOSS",
            "net_pnl": -0.9,
            "current_price": 0.5,
            "side": "YES",
            "generated_at": datetime(2026, 7, 20, tzinfo=UTC),
        }
        for _ in range(10)
    ]

    async def _fetch(query, *args):
        if "detector_state" in query or "DISTINCT scope" in query:
            return []
        if "detector_signals" in query:
            return outcome_rows_baseline + outcome_rows_recent
        return [{"id": 42}]

    publish_mock = AsyncMock(return_value=PublishResult(sent=1))
    with (
        patch("app.jobs.edge_decay_monitor.fetchrow", new=AsyncMock(return_value=None)),
        patch("app.jobs.edge_decay_monitor.fetch_active_detectors", new=AsyncMock(return_value={"D3"})),
        patch("app.jobs.edge_decay_monitor.fetch", side_effect=_fetch),
        patch("app.jobs.edge_decay_monitor.execute", new=AsyncMock(return_value="INSERT 0 1")),
        patch("app.jobs.edge_decay_monitor.publish", publish_mock),
        patch("app.jobs.edge_decay_monitor._now", return_value=datetime(2026, 7, 21, tzinfo=UTC)),
    ):
        result = await run_edge_decay_monitor()

    assert result["n_entities"] == 1
    assert result["n_triggered"] == 1
    publish_mock.assert_awaited_once()
    published_notifications = publish_mock.await_args.args[0]
    assert len(published_notifications) == 1
    assert "D3" in published_notifications[0].subject
    assert publish_mock.await_args.kwargs["actor"] == "edge_decay_monitor"


@pytest.mark.asyncio
async def test_run_edge_decay_monitor_no_tg_call_when_nothing_decaying():
    stable_rows = [
        {
            "outcome": "WIN" if i % 2 == 0 else "LOSS",
            "net_pnl": 0.1 if i % 2 == 0 else -0.1,
            "current_price": 0.5,
            "side": "YES",
            "generated_at": datetime(2026, 5, 1, tzinfo=UTC) if i < 20 else datetime(2026, 7, 20, tzinfo=UTC),
        }
        for i in range(30)
    ]

    async def _fetch(query, *args):
        if "detector_state" in query or "DISTINCT scope" in query:
            return []
        if "detector_signals" in query:
            return stable_rows
        return [{"id": 1}]

    publish_mock = AsyncMock(return_value=PublishResult())
    with (
        patch("app.jobs.edge_decay_monitor.fetchrow", new=AsyncMock(return_value=None)),
        patch("app.jobs.edge_decay_monitor.fetch_active_detectors", new=AsyncMock(return_value={"D3"})),
        patch("app.jobs.edge_decay_monitor.fetch", side_effect=_fetch),
        patch("app.jobs.edge_decay_monitor.execute", new=AsyncMock(return_value="INSERT 0 1")),
        patch("app.jobs.edge_decay_monitor.publish", publish_mock),
        patch("app.jobs.edge_decay_monitor._now", return_value=datetime(2026, 7, 21, tzinfo=UTC)),
    ):
        result = await run_edge_decay_monitor()

    assert result["n_triggered"] == 0
    publish_mock.assert_not_awaited()
