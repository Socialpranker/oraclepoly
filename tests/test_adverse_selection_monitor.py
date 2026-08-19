"""Tests для app.jobs.adverse_selection_monitor."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.analytics.adverse_selection import AdverseCell, AdverseSelectionConfig
from app.jobs.adverse_selection_monitor import (
    _adverse_set,
    _build_cells,
    _CellRow,
    _load_config,
    _previous_adverse_set,
    run_adverse_selection_monitor,
)
from app.services.notification_bus import PublishResult


@pytest.mark.asyncio
async def test_load_config_returns_row_values():
    row = {"lookback_days": 60, "min_events": 20, "wilson_threshold": 0.50}
    with patch("app.jobs.adverse_selection_monitor.fetchrow", new=AsyncMock(return_value=row)):
        config = await _load_config()
    assert config == AdverseSelectionConfig(min_events=20, wilson_threshold=0.5)


@pytest.mark.asyncio
async def test_load_config_falls_back_to_defaults_when_empty():
    with patch("app.jobs.adverse_selection_monitor.fetchrow", new=AsyncMock(return_value=None)):
        config = await _load_config()
    assert config == AdverseSelectionConfig(min_events=20, wilson_threshold=0.5)


def test_build_cells_groups_by_detector_category_and_flags_temperature():
    cfg = AdverseSelectionConfig(min_events=2, wilson_threshold=0.5)
    rows = [
        # D38 × W&E, temperature-событие, оба LOSS → adverse при min_events=2
        _CellRow(detector="D38", category="World & Economy", outcome="LOSS", net_pnl=-40.0, is_temperature=True),
        _CellRow(detector="D38", category="World & Economy", outcome="LOSS", net_pnl=-48.0, is_temperature=True),
        # D3 × Sports, оба WIN → не adverse
        _CellRow(detector="D3", category="Sports", outcome="WIN", net_pnl=60.0, is_temperature=False),
        _CellRow(detector="D3", category="Sports", outcome="WIN", net_pnl=55.0, is_temperature=False),
    ]
    cells = _build_cells(rows, cfg)
    by_key = {(c.detector, c.category): c for c in cells}
    d38 = by_key[("D38", "World & Economy")]
    assert d38.is_adverse is True
    assert d38.is_temperature is True
    assert d38.n_events == 2
    assert d38.net_pnl == -88.0
    d3 = by_key[("D3", "Sports")]
    assert d3.is_adverse is False
    assert d3.is_temperature is False


def test_build_cells_collapses_mixed_events_to_one_outcome_each():
    # 3 события в одной ячейке: 2 LOSS + 1 WIN → wins=1/3, wilson_lower≈0.0615 (< 0.5),
    # net_pnl суммарно отрицателен → adverse при min_events=3. Числа детерминированы
    # (не round number типа "0.5 ровно"): подтверждено прогоном compute_window_metrics.
    cfg = AdverseSelectionConfig(min_events=3, wilson_threshold=0.5)
    rows = [
        _CellRow(detector="D42", category="Crypto", outcome="LOSS", net_pnl=-40.0, is_temperature=False),
        _CellRow(detector="D42", category="Crypto", outcome="LOSS", net_pnl=-30.0, is_temperature=False),
        _CellRow(detector="D42", category="Crypto", outcome="WIN", net_pnl=20.0, is_temperature=False),
    ]
    cells = _build_cells(rows, cfg)
    assert len(cells) == 1
    cell = cells[0]
    assert cell.n_events == 3
    assert cell.wins == 1
    assert cell.net_pnl == -50.0
    assert cell.wilson_lower == pytest.approx(0.0615, abs=1e-3)
    assert cell.wilson_lower < cfg.wilson_threshold
    assert cell.is_adverse is True


def test_adverse_set_returns_only_adverse_keys():
    cells = [
        AdverseCell("D38", "World & Economy", 30, 11, 0.37, 0.55, -88.0, True, True),
        AdverseCell("D3", "Sports", 30, 22, 0.55, 0.85, 140.0, False, False),
    ]
    assert _adverse_set(cells) == frozenset({"D38×World & Economy"})


@pytest.mark.asyncio
async def test_previous_adverse_set_reads_manifest_metrics():
    manifest_row = {"metrics": json.dumps({"adverse_set": ["D38×World & Economy"]})}
    with patch("app.jobs.adverse_selection_monitor.fetchrow", new=AsyncMock(return_value=manifest_row)):
        result = await _previous_adverse_set()
    assert result == frozenset({"D38×World & Economy"})


@pytest.mark.asyncio
async def test_previous_adverse_set_empty_when_no_manifest():
    with patch("app.jobs.adverse_selection_monitor.fetchrow", new=AsyncMock(return_value=None)):
        result = await _previous_adverse_set()
    assert result == frozenset()


@pytest.mark.asyncio
async def test_run_sends_tg_when_adverse_set_changed():
    cells = [AdverseCell("D38", "World & Economy", 30, 11, 0.37, 0.55, -88.0, True, True)]
    send_mock = AsyncMock(return_value=PublishResult(sent=1))
    with (
        patch(
            "app.jobs.adverse_selection_monitor._load_config",
            new=AsyncMock(return_value=AdverseSelectionConfig(20, 0.5)),
        ),
        patch("app.jobs.adverse_selection_monitor._load_lookback_days", new=AsyncMock(return_value=60)),
        patch("app.jobs.adverse_selection_monitor._fetch_cell_rows", new=AsyncMock(return_value=[])),
        patch("app.jobs.adverse_selection_monitor._build_cells", return_value=cells),
        patch("app.jobs.adverse_selection_monitor._persist_cell_run", new=AsyncMock(return_value=1)),
        patch("app.jobs.adverse_selection_monitor._persist_claim_if_adverse", new=AsyncMock()),
        patch("app.jobs.adverse_selection_monitor._previous_adverse_set", new=AsyncMock(return_value=frozenset())),
        patch("app.jobs.adverse_selection_monitor._persist_manifest", new=AsyncMock()),
        # Мокаем шину: джоба отдаёт структуру, рассылку админам делает publish.
        patch("app.jobs.adverse_selection_monitor.publish", new=send_mock),
    ):
        result = await run_adverse_selection_monitor()
    assert result["n_adverse"] == 1
    assert result["tg_sent"] is True
    send_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_throttles_tg_when_adverse_set_unchanged():
    cells = [AdverseCell("D38", "World & Economy", 30, 11, 0.37, 0.55, -88.0, True, True)]
    send_mock = AsyncMock()
    with (
        patch(
            "app.jobs.adverse_selection_monitor._load_config",
            new=AsyncMock(return_value=AdverseSelectionConfig(20, 0.5)),
        ),
        patch("app.jobs.adverse_selection_monitor._load_lookback_days", new=AsyncMock(return_value=60)),
        patch("app.jobs.adverse_selection_monitor._fetch_cell_rows", new=AsyncMock(return_value=[])),
        patch("app.jobs.adverse_selection_monitor._build_cells", return_value=cells),
        patch("app.jobs.adverse_selection_monitor._persist_cell_run", new=AsyncMock(return_value=1)),
        patch("app.jobs.adverse_selection_monitor._persist_claim_if_adverse", new=AsyncMock()),
        patch(
            "app.jobs.adverse_selection_monitor._previous_adverse_set",
            new=AsyncMock(return_value=frozenset({"D38×World & Economy"})),
        ),
        patch("app.jobs.adverse_selection_monitor._persist_manifest", new=AsyncMock()),
        # Мокаем шину: джоба отдаёт структуру, рассылку админам делает publish.
        patch("app.jobs.adverse_selection_monitor.publish", new=send_mock),
    ):
        result = await run_adverse_selection_monitor()
    assert result["tg_sent"] is False
    send_mock.assert_not_awaited()
