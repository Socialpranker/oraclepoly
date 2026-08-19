"""Smoke-тесты helpers из scripts/backfill_detector_signals.py."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "backfill_detector_signals.py"

spec = importlib.util.spec_from_file_location("backfill_detector_signals", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_split_into_windows_aligns_to_end():
    start = datetime(2026, 4, 30, 0, 0, tzinfo=UTC)
    end = datetime(2026, 4, 30, 13, 0, tzinfo=UTC)  # 13 часов
    windows = module._split_into_windows(start, end, hours=6)
    assert len(windows) == 3
    # последнее окно усечено до 13:00, не 18:00
    last_start, last_end = windows[-1]
    assert datetime.fromtimestamp(last_end, tz=UTC) == end


def test_split_into_windows_empty_when_zero_range():
    start = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
    windows = module._split_into_windows(start, start, hours=6)
    assert windows == []


def test_split_into_windows_unix_seconds():
    start = datetime(2026, 4, 30, 0, 0, tzinfo=UTC)
    end = datetime(2026, 4, 30, 6, 0, tzinfo=UTC)
    windows = module._split_into_windows(start, end, hours=6)
    assert windows == [(int(start.timestamp()), int(end.timestamp()))]


def test_normalize_to_d1_payload_basic():
    event = {
        "transactionHash": "0xabc",
        "proxyWallet": "0xWHALE",
        "usdcSize": 12000,
        "side": "BUY",
        "outcome": "Yes",
        "price": 0.65,
    }
    payload = module._normalize_to_d1_payload(event, market_id="mkt-1")
    assert payload["tx_hash"] == "0xabc"
    assert payload["from_address"] == "0xwhale"
    assert payload["amount"] == "12000"
    assert payload["direction"] == "buy"
    assert payload["outcome"] == "Yes"
    assert payload["price"] == "0.65"
    assert payload["market_id"] == "mkt-1"
    assert payload["source"] == "backfill_activity"


def test_normalize_handles_missing_fields():
    event = {"transactionHash": "0x1", "proxyWallet": ""}
    payload = module._normalize_to_d1_payload(event, market_id="mkt")
    assert payload["amount"] == "0"
    assert payload["direction"] == ""
    assert payload["outcome"] == ""
