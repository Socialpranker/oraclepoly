"""Tests for app.jobs.mtm_refresh — Phase 1 Stage 1.

Покрываем pure-function слой:
  - _row_to_snapshot: маппинг DB row → MtMPositionSnapshot (с side-adjusted entry).
  - _build_updates: per-order (order_id, mtm_price, unrealized_pnl, mtm_updated_at).
  - SQL constants (статусы open positions, JOIN markets).
DB-моки минимальны — тестируем contract, не транспортный слой.
"""

from __future__ import annotations

import pytest

from app.core.bankroll_mtm import MtMPositionSnapshot, compute_bankroll_with_mtm
from app.jobs.mtm_refresh import (
    OPEN_POSITION_STATUSES,
    OPEN_POSITIONS_SQL,
    _build_update_rows,
    _row_to_snapshot,
)

# --- _row_to_snapshot ----------------------------------------------------


def test_row_to_snapshot_yes_side():
    row = {
        "id": "ord-abc",
        "side": "YES",
        "stake": 100.0,
        "fill_price": 0.40,
        "current_price": 0.55,
    }
    snap = _row_to_snapshot(row)
    assert snap.order_id == "ord-abc"
    assert snap.side == "YES"
    assert snap.stake == 100.0
    assert snap.entry_price == 0.40
    assert snap.current_price == 0.55


def test_row_to_snapshot_no_side_uses_fill_price_directly():
    """fill_price для NO side уже side-adjusted (1 − YES_at_fill).
    См. SSoT app/jobs/position_manager.py:151-153. Не пересчитываем."""
    row = {
        "id": "ord-no",
        "side": "NO",
        "stake": 100.0,
        "fill_price": 0.60,  # entry NO = 1 - 0.40 (YES_at_fill=0.40)
        "current_price": 0.50,  # YES now
    }
    snap = _row_to_snapshot(row)
    assert snap.side == "NO"
    assert snap.entry_price == 0.60  # без преобразований
    assert snap.current_price == 0.50


def test_row_to_snapshot_none_current_price_preserved():
    """markets.current_price NULL → snapshot.current_price = None.
    bankroll_mtm warning + unrealized=0 для этой позиции."""
    row = {
        "id": "ord-stale",
        "side": "YES",
        "stake": 50.0,
        "fill_price": 0.30,
        "current_price": None,
    }
    snap = _row_to_snapshot(row)
    assert snap.current_price is None


def test_row_to_snapshot_id_coerced_to_string():
    """DB id может быть UUID — приводим к str для consistent keying."""
    import uuid

    uid = uuid.UUID("12345678-1234-5678-1234-567812345678")
    row = {
        "id": uid,
        "side": "YES",
        "stake": 100.0,
        "fill_price": 0.5,
        "current_price": 0.5,
    }
    snap = _row_to_snapshot(row)
    assert snap.order_id == str(uid)


# --- _build_update_rows --------------------------------------------------


def test_build_update_rows_yes_winning():
    """YES @ entry=0.20, current=0.40. shares=100/0.20=500. equity=500*0.40=$200.
    unrealized = $200 − $100 = +$100."""
    rows = [
        {
            "id": "ord-1",
            "side": "YES",
            "stake": 100.0,
            "fill_price": 0.20,
            "current_price": 0.40,
        }
    ]
    updates = _build_update_rows(rows)
    assert len(updates) == 1
    upd = updates[0]
    assert upd["order_id"] == "ord-1"
    assert upd["mtm_price"] == pytest.approx(0.40)
    assert upd["unrealized_pnl"] == pytest.approx(100.0)
    # mtm_updated_at — timestamp заполняется внутри job (datetime.now(UTC))
    assert "mtm_updated_at" in upd


def test_build_update_rows_no_losing():
    """NO @ entry=0.60 (YES_fill=0.40), current YES=0.80 → NO_now=0.20.
    shares = 100/0.60 ≈ 166.67. equity = 166.67 * 0.20 ≈ $33.33. PnL = -$66.67."""
    rows = [
        {
            "id": "ord-2",
            "side": "NO",
            "stake": 100.0,
            "fill_price": 0.60,
            "current_price": 0.80,
        }
    ]
    updates = _build_update_rows(rows)
    assert len(updates) == 1
    assert updates[0]["unrealized_pnl"] == pytest.approx(-66.67, abs=0.01)
    assert updates[0]["mtm_price"] == pytest.approx(0.80)


def test_build_update_rows_skips_missing_current_price():
    """current_price=NULL → пропускаем строку (warning), не пишем mtm_updated_at.
    Conservative: stale данные → check_daily_pnl_limit_mtm должен трактовать
    эту позицию консервативно (skip)."""
    rows = [
        {
            "id": "ord-3",
            "side": "YES",
            "stake": 100.0,
            "fill_price": 0.30,
            "current_price": None,
        }
    ]
    updates = _build_update_rows(rows)
    assert updates == []


def test_build_update_rows_skips_bad_entry_price():
    """fill_price <= 0 (corrupted data) → skip."""
    rows = [
        {
            "id": "ord-bad",
            "side": "YES",
            "stake": 100.0,
            "fill_price": 0.0,
            "current_price": 0.5,
        }
    ]
    updates = _build_update_rows(rows)
    assert updates == []


def test_build_update_rows_mixed_positions():
    rows = [
        {
            "id": "ord-w",
            "side": "YES",
            "stake": 200.0,
            "fill_price": 0.25,
            "current_price": 0.50,
        },
        {
            "id": "ord-l",
            "side": "NO",
            "stake": 100.0,
            "fill_price": 0.30,
            "current_price": 0.80,
        },
        {
            "id": "ord-skip",
            "side": "YES",
            "stake": 50.0,
            "fill_price": 0.50,
            "current_price": None,
        },
    ]
    updates = _build_update_rows(rows)
    assert len(updates) == 2
    by_id = {u["order_id"]: u for u in updates}
    # ord-w: shares=200/0.25=800, equity=800*0.5=400, pnl=+200
    assert by_id["ord-w"]["unrealized_pnl"] == pytest.approx(200.0)
    # ord-l: shares=100/0.30≈333.33, equity=333.33*0.20≈66.67, pnl=-33.33
    assert by_id["ord-l"]["unrealized_pnl"] == pytest.approx(-33.33, abs=0.01)
    assert "ord-skip" not in by_id


def test_build_update_rows_consistency_with_bankroll_mtm():
    """_build_update_rows должен использовать ту же математику что и
    compute_bankroll_with_mtm. Cross-check: sum(updates.unrealized_pnl) ==
    BankrollMtMResult.unrealized_pnl."""
    rows = [
        {"id": "a", "side": "YES", "stake": 100.0, "fill_price": 0.40, "current_price": 0.20},
        {"id": "b", "side": "NO", "stake": 100.0, "fill_price": 0.60, "current_price": 0.40},
    ]
    updates = _build_update_rows(rows)
    sum_updates = sum(u["unrealized_pnl"] for u in updates)

    snapshots = [
        MtMPositionSnapshot(
            order_id=r["id"],
            stake=r["stake"],
            entry_price=r["fill_price"],
            current_price=r["current_price"],
            side=r["side"],
        )
        for r in rows
    ]
    result = compute_bankroll_with_mtm(
        wallet_balance=0.0, frozen_stake=0.0, open_positions=snapshots
    )
    assert sum_updates == pytest.approx(result.unrealized_pnl, abs=0.001)


# --- SQL constants -------------------------------------------------------


def test_open_position_statuses_includes_virtual_and_live():
    """MtM трекает обе категории: virtual и real placed/filled."""
    assert "placed" in OPEN_POSITION_STATUSES
    assert "filled" in OPEN_POSITION_STATUSES
    assert "virtual_placed" in OPEN_POSITION_STATUSES
    assert "virtual_filled" in OPEN_POSITION_STATUSES


def test_open_positions_sql_filters_unsettled():
    """Query select только settled_at IS NULL."""
    assert "settled_at IS NULL" in OPEN_POSITIONS_SQL


def test_open_positions_sql_joins_markets_for_current_price():
    """Должны JOIN markets чтобы получить canonical YES current_price."""
    assert "JOIN markets" in OPEN_POSITIONS_SQL or "join markets" in OPEN_POSITIONS_SQL.lower()
    assert "current_price" in OPEN_POSITIONS_SQL


def test_open_positions_sql_excludes_resolved_markets():
    """Resolved markets не нуждаются в MtM — settle_filled закроет."""
    assert "resolved" in OPEN_POSITIONS_SQL.lower()
