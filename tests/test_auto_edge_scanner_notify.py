"""Auto Edge Scanner — notify unit tests."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts.research.edge_scanner.gate import CellResult
from scripts.research.edge_scanner.grid_generator import Cell
from scripts.research.edge_scanner.notify import (
    build_alert_message,
    send_alerts,
)
from scripts.research.edge_scanner.persist import ClassifiedResult


def _make_cell(**overrides) -> Cell:
    base = dict(
        feature="smart_money_taker_share",
        threshold_op=">",
        threshold_value=0.5,
        threshold_quantile_name="q50",
        zone="extreme",
        zone_lo=0.85,
        zone_hi=0.97,
        anchor=24,
        category="Politics",
        side="NO",
    )
    base.update(overrides)
    return Cell(**base)


def _make_result(**overrides) -> CellResult:
    base = dict(
        cell=_make_cell(),
        n_signals=842,
        n_unique_markets=520,
        win_rate=0.645,
        avg_cost=0.10,
        net_roi=0.087,
        mirror_net_roi=-0.123,
        mirror_diff_pp=21.0,
        year_2025_net_roi=0.11,
        year_2026_net_roi=0.065,
        year_consistency=0.59,
        daily_signals=1.03,
        daily_raw_pnl=2.30,
        daily_honest_pnl=1.15,
        p_value=0.001,
        bh_adjusted_p=0.018,
        verdict="MARGINAL",
    )
    base.update(overrides)
    return CellResult(**base)


def _make_classified(**overrides) -> ClassifiedResult:
    base = dict(
        result=_make_result(),
        status="novel",
        overlaps={"D7b": 0.12, "D13": 0.08, "D14": 0.0, "D5": 0.0, "D3": 0.0, "D10b": 0.0},
        overlap_max=0.12,
        rank_score=0.85,
        cell_signature="abc123",
        first_seen_scan_date=date(2026, 5, 10),
        n_times_seen=1,
    )
    base.update(overrides)
    return ClassifiedResult(**base)


# ─── build_alert_message ─────────────────────────────────────────────────────


def test_html_escape_on_user_inputs():
    """Feature name with HTML chars → escaped."""
    cell = _make_cell(feature="suspicious<feature>")
    cr = _make_classified(result=_make_result(cell=cell))
    msg = build_alert_message(cr, "report.md", rank=1)
    assert "<feature>" not in msg  # raw HTML stripped
    assert "&lt;feature&gt;" in msg


def test_message_contains_key_metrics():
    cr = _make_classified()
    msg = build_alert_message(cr, "report.md", rank=1)
    assert "842" in msg  # n_signals
    assert "520" in msg  # n_unique_markets
    assert "+8.7%" in msg  # net_roi
    assert "+21.0pp" in msg  # mirror_diff
    assert "MARGINAL" in msg  # verdict


def test_message_includes_sustained_flag():
    """If n_times_seen >= 2, message should say 'sustained'."""
    cr = _make_classified(n_times_seen=3)
    msg = build_alert_message(cr, "report.md", rank=1)
    assert "sustained" in msg.lower() or "3×" in msg


def test_message_no_sustained_for_first_seen():
    cr = _make_classified(n_times_seen=1)
    msg = build_alert_message(cr, "report.md", rank=1)
    assert "sustained" not in msg.lower()


def test_message_includes_year_split():
    cr = _make_classified()
    msg = build_alert_message(cr, "report.md", rank=1)
    assert "2025" in msg
    assert "2026" in msg


def test_message_handles_missing_year_split():
    """If year_*_net_roi is None, message should not crash."""
    cr = _make_classified(result=_make_result(year_2025_net_roi=None, year_2026_net_roi=None))
    msg = build_alert_message(cr, "report.md", rank=1)
    assert msg is not None
    assert "Stats:" in msg


def test_message_includes_fdr_q_value():
    cr = _make_classified()
    msg = build_alert_message(cr, "report.md", rank=1)
    assert "q-value" in msg.lower() or "0.018" in msg


# ─── send_alerts ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_message_when_no_novel():
    """Empty list → 0 alerts."""
    mock_conn = MagicMock()
    mock_send = AsyncMock()
    sent = await send_alerts(mock_conn, "scan-id", [], "report.md", send_tg=True, send_admin_message_fn=mock_send)
    assert sent == 0
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_no_message_when_all_subset():
    """All status='subset' → 0 alerts."""
    classified = [_make_classified(status="subset"), _make_classified(status="subset")]
    mock_conn = MagicMock()
    mock_conn.fetchrow = AsyncMock(return_value=None)
    mock_send = AsyncMock()
    sent = await send_alerts(mock_conn, "scan-id", classified, "report.md", send_tg=True, send_admin_message_fn=mock_send)
    assert sent == 0
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_dry_run_no_send():
    """send_tg=False → no actual send call, returns 0."""
    classified = [_make_classified(status="novel")]
    mock_conn = MagicMock()
    mock_conn.fetchrow = AsyncMock(return_value=None)
    mock_send = AsyncMock()
    sent = await send_alerts(mock_conn, "scan-id", classified, "report.md", send_tg=False, send_admin_message_fn=mock_send)
    assert sent == 0
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_top_3_only_per_run():
    """Feed 8 novel → only top 3 sent."""
    # Higher rank_score = higher priority
    classified = [_make_classified(status="novel", rank_score=float(i), cell_signature=f"sig_{i}") for i in range(8)]
    mock_conn = MagicMock()
    mock_conn.fetchrow = AsyncMock(return_value=None)  # not previously notified
    mock_conn.execute = AsyncMock()
    mock_send = AsyncMock()
    sent = await send_alerts(mock_conn, "scan-id", classified, "report.md", send_tg=True, send_admin_message_fn=mock_send)
    assert sent == 3


@pytest.mark.asyncio
async def test_cross_week_suppression():
    """Signature already notified → suppressed."""
    classified = [_make_classified(status="novel", cell_signature="sig_old")]
    mock_conn = MagicMock()
    mock_conn.fetchrow = AsyncMock(return_value={"1": 1})  # notified previously
    mock_conn.execute = AsyncMock()
    mock_send = AsyncMock()
    sent = await send_alerts(mock_conn, "scan-id", classified, "report.md", send_tg=True, send_admin_message_fn=mock_send)
    assert sent == 0
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_marks_notified_after_send():
    """After successful send, conn.execute called с _MARK_NOTIFIED_SQL."""
    classified = [_make_classified(status="novel", cell_signature="sig_new")]
    mock_conn = MagicMock()
    mock_conn.fetchrow = AsyncMock(return_value=None)
    mock_conn.execute = AsyncMock()
    mock_send = AsyncMock()
    sent = await send_alerts(mock_conn, "scan-id", classified, "report.md", send_tg=True, send_admin_message_fn=mock_send)
    assert sent == 1
    # Verify execute was called (mark_notified)
    mock_conn.execute.assert_called_once()
    # Check signature was passed
    call_args = mock_conn.execute.call_args
    assert "sig_new" in call_args.args


@pytest.mark.asyncio
async def test_only_pass_or_marginal_eligible():
    """Verdict='FAIL' → not eligible even if status='novel'."""
    classified = [_make_classified(status="novel", result=_make_result(verdict="FAIL"))]
    mock_conn = MagicMock()
    mock_conn.fetchrow = AsyncMock(return_value=None)
    mock_send = AsyncMock()
    sent = await send_alerts(mock_conn, "scan-id", classified, "report.md", send_tg=True, send_admin_message_fn=mock_send)
    assert sent == 0
