"""Fix-2 (latency-аудит №3): source-tag daemon/cron + лаг-мониторинг first-row p50.

Часть A — source-tag. Демон и cron пишут в execution_orders через один
_insert_order, источник нигде не фиксируется — по строкам БД нельзя понять,
кто выиграл гонку и какой у каждого пути реальный лаг. Тег кладётся в
meta jsonb (колонка есть, писалась всегда '{}') централизованно в
_insert_order через contextvar EXECUTION_SOURCE: default 'cron', демон ставит
'daemon' в process_one.

Часть B — лаг signal→order. Решение аудита №3: лаг меряем ТОЛЬКО по первой
строке сигнала (MIN created_at per detector_signal_id) — retry-строки
transient-reject'ов раздували avg в 40×. Джоба execution_lag_monitor считает
first-row p50/p95 за 24ч с разбивкой по meta->>'source'.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

import app.jobs.execution_lag_monitor as lag_mod
import app.jobs.v2_execution as v2
from app.jobs.execution_lag_monitor import _LAG_SQL, run_execution_lag_monitor
from app.jobs.v2_execution import EXECUTION_SOURCE, _insert_order


def _order_payload(**overrides):
    p = {
        "detector": "D3_cross_mkt_arb",
        "detector_signal_id": "00000000-0000-0000-0000-000000000010",
        "market_id": "0xM",
        "event_id": "0xE",
        "side": "YES",
        "execution_mode": "virtual",
        "signal_price": 0.40,
        "calibrated_p": 0.55,
        "meta_p": 0.55,
        "ev_pct": 5.0,
        "order_type": "taker",
        "stake": 10.0,
        "bankroll_at_entry": 1000.0,
        "status": "placed",
    }
    p.update(overrides)
    return p


def _capture_fetchrow(monkeypatch):
    captured = {}

    async def _fake_fetchrow(sql, *args):
        captured["sql"] = sql
        captured["args"] = args
        return {"id": "row-1"}

    monkeypatch.setattr(v2, "fetchrow", _fake_fetchrow)
    return captured


def _meta_param(captured) -> dict:
    # _INSERT_ORDER_SQL: meta = $17 → args[16] (args без sql)
    return json.loads(captured["args"][16])


# ---------- Часть A: source-tag в _insert_order ----------


@pytest.mark.asyncio
async def test_insert_order_tags_source_cron_by_default(monkeypatch):
    """Без явного set — cron-путь (run_v2_execution не трогает contextvar)."""
    captured = _capture_fetchrow(monkeypatch)

    await _insert_order(_order_payload())

    assert _meta_param(captured)["source"] == "cron"


@pytest.mark.asyncio
async def test_insert_order_tags_source_daemon_when_contextvar_set(monkeypatch):
    captured = _capture_fetchrow(monkeypatch)

    token = EXECUTION_SOURCE.set("daemon")
    try:
        await _insert_order(_order_payload())
    finally:
        EXECUTION_SOURCE.reset(token)

    assert _meta_param(captured)["source"] == "daemon"


@pytest.mark.asyncio
async def test_insert_order_preserves_explicit_meta_keys(monkeypatch):
    """Явный payload['meta'] не затирается тегом (merge, payload приоритетнее)."""
    captured = _capture_fetchrow(monkeypatch)

    await _insert_order(_order_payload(meta={"gate_shadow": True}))

    meta = _meta_param(captured)
    assert meta["gate_shadow"] is True
    assert meta["source"] == "cron"


@pytest.mark.asyncio
async def test_daemon_process_one_sets_source_daemon():
    """process_one демона проставляет contextvar до _process_signal."""
    from scripts.execution_daemon import process_one

    sig = {"detector": "D3_cross_mkt_arb", "detector_signal_id": "sig-1", "token_id": "tok-1"}
    seen = {}

    async def _spy(s):
        seen["source"] = v2.EXECUTION_SOURCE.get()
        return "opened"

    with (
        patch(
            "scripts.execution_daemon.fetch_calibrated_signal_by_id",
            AsyncMock(return_value=sig),
        ),
        patch("app.jobs.v2_execution._process_signal", _spy),
    ):
        result = await process_one("sig-1")

    assert result == "opened"
    assert seen["source"] == "daemon"


# ---------- Часть B: лаг-мониторинг first-row p50 ----------


def test_lag_sql_uses_first_row_semantics():
    """Ядро решения аудита: одна строка на сигнал — первая по created_at.
    Без этого retry-строки transient-reject'ов раздувают avg в 40×."""
    assert "DISTINCT ON (eo.detector_signal_id)" in _LAG_SQL
    assert "ORDER BY eo.detector_signal_id, eo.created_at" in _LAG_SQL
    assert "PERCENTILE_CONT(0.5)" in _LAG_SQL


@pytest.mark.asyncio
async def test_run_execution_lag_monitor_returns_per_source_stats(monkeypatch):
    rows = [
        {"source": "cron", "n_signals": 40, "lag_p50_sec": 38.5, "lag_p95_sec": 120.0},
        {"source": "daemon", "n_signals": 12, "lag_p50_sec": 1.2, "lag_p95_sec": 4.0},
    ]
    monkeypatch.setattr(lag_mod, "fetch", AsyncMock(return_value=rows))

    result = await run_execution_lag_monitor()

    assert result["sources"]["daemon"]["lag_p50_sec"] == 1.2
    assert result["sources"]["cron"]["n_signals"] == 40
    assert result["window_hours"] == 24


@pytest.mark.asyncio
async def test_run_execution_lag_monitor_empty_db(monkeypatch):
    """Пустая выборка (нет ордеров за окно) → пустой sources, не падаем."""
    monkeypatch.setattr(lag_mod, "fetch", AsyncMock(return_value=[]))

    result = await run_execution_lag_monitor()

    assert result["sources"] == {}


def test_cron_runner_registers_execution_lag_monitor():
    import importlib
    import sys

    if "scripts.cron_runner" in sys.modules:
        del sys.modules["scripts.cron_runner"]
    runner = importlib.import_module("scripts.cron_runner")
    assert "execution-lag-monitor" in runner.COMMANDS
