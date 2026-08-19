"""Tests for D36 OBI Pressure detector."""

from __future__ import annotations

from app.detectors import d36_obi_pressure
from app.detectors.d36_obi_pressure import (
    MAX_SPREAD,
    MIN_OBI_ABS,
    MIN_TOTAL_DEPTH_USD,
    compute_obi_metrics,
    detect,
)


def _make_book(bid_depth: float, ask_depth: float, bid_price: float = 0.55, ask_price: float = 0.57) -> dict:
    """Helper для синтетического orderbook."""
    return {
        "bids": [{"price": str(bid_price), "size": str(bid_depth / bid_price)}],
        "asks": [{"price": str(ask_price), "size": str(ask_depth / ask_price)}],
    }


def _make_event(yes_price: float = 0.55, ttr: float = 24.0, book_metrics: dict | None = None) -> dict:
    return {
        "market_id": "0xabc",
        "category": "Sports",
        "current_price": yes_price,
        "hours_to_resolve": ttr,
        "clob_token_ids": ["yes_token_xyz", "no_token_xyz"],
        "event_id": "event_123",
        "book_metrics": book_metrics,
    }


# --- compute_obi_metrics --------------------------------------------------


def test_obi_balanced_book_zero():
    """Equal bid/ask depth → OBI = 0."""
    book = _make_book(bid_depth=10000, ask_depth=10000)
    m = compute_obi_metrics(book)
    assert m is not None
    assert abs(m["obi"]) < 0.01
    assert m["total_depth_usd"] == 20000.0


def test_obi_heavy_bid():
    """75/25 bid/ask → OBI = +0.5."""
    book = _make_book(bid_depth=15000, ask_depth=5000)
    m = compute_obi_metrics(book)
    assert m is not None
    assert m["obi"] == 0.5


def test_obi_heavy_ask():
    """25/75 bid/ask → OBI = −0.5."""
    book = _make_book(bid_depth=5000, ask_depth=15000)
    m = compute_obi_metrics(book)
    assert m["obi"] == -0.5


def test_obi_empty_book_returns_none():
    """Empty bids → None."""
    assert compute_obi_metrics({"bids": [], "asks": [{"price": "0.5", "size": "100"}]}) is None
    assert compute_obi_metrics({"bids": [{"price": "0.5", "size": "100"}], "asks": []}) is None


def test_obi_inverted_book_returns_none():
    """bid >= ask is invalid (crossed book) → None."""
    bad = {
        "bids": [{"price": "0.60", "size": "1000"}],
        "asks": [{"price": "0.55", "size": "1000"}],
    }
    assert compute_obi_metrics(bad) is None


# --- detect ---------------------------------------------------------------


def test_detect_emits_signal_strong_bid_pressure():
    """OBI=0.5, spread=0.02, depth=$20K, mid-zone → emit YES."""
    book = _make_book(bid_depth=15000, ask_depth=5000)
    metrics = compute_obi_metrics(book)
    sig = detect(_make_event(yes_price=0.55, book_metrics=metrics))
    assert sig is not None
    assert sig.side == "YES"
    assert sig.detector == d36_obi_pressure.DETECTOR_NAME
    assert sig.evidence["obi"] == 0.5


def test_detect_skip_negative_obi():
    """Negative OBI (ask pressure) → no signal.

    NO-side filter (2026-05-22): virtual v2 показал inverse edge на NO side
    (NO -$1,305 / n=7 vs YES +$210 / n=9). До формального backtest с
    инверсией знака emit только YES signals.
    """
    book = _make_book(bid_depth=5000, ask_depth=15000)
    metrics = compute_obi_metrics(book)
    sig = detect(_make_event(yes_price=0.55, book_metrics=metrics))
    assert sig is None


def test_detect_skip_weak_obi():
    """|OBI| < 0.25 → no signal."""
    book = _make_book(bid_depth=10500, ask_depth=9500)  # ~5% asymmetry
    metrics = compute_obi_metrics(book)
    sig = detect(_make_event(book_metrics=metrics))
    assert sig is None


def test_detect_skip_wide_spread():
    """Spread > 8¢ → no signal (toxic withdrawal)."""
    book = _make_book(bid_depth=15000, ask_depth=5000, bid_price=0.50, ask_price=0.65)
    metrics = compute_obi_metrics(book)
    sig = detect(_make_event(book_metrics=metrics))
    assert sig is None


def test_detect_skip_thin_book():
    """Total depth < $5K → no signal (защита от манипуляции)."""
    book = _make_book(bid_depth=3000, ask_depth=1000)
    metrics = compute_obi_metrics(book)
    sig = detect(_make_event(book_metrics=metrics))
    assert sig is None


def test_detect_skip_extreme_zone():
    """Price вне [0.30, 0.60] → no signal.

    Tightened 2026-05-20: было [0.30, 0.85], но favorite-fade NO trades
    в yes ∈ [0.60, 0.85] показали WR 0-3% / -$1,376 на virtual n=39.
    """
    book = _make_book(bid_depth=15000, ask_depth=5000)
    metrics = compute_obi_metrics(book)
    assert detect(_make_event(yes_price=0.90, book_metrics=metrics)) is None
    assert detect(_make_event(yes_price=0.20, book_metrics=metrics)) is None
    # Новые boundary tests (favorite-fade zone reject)
    assert detect(_make_event(yes_price=0.70, book_metrics=metrics)) is None
    assert detect(_make_event(yes_price=0.65, book_metrics=metrics)) is None


def test_detect_skip_short_ttr():
    """TTR < 4h → no signal."""
    book = _make_book(bid_depth=15000, ask_depth=5000)
    metrics = compute_obi_metrics(book)
    sig = detect(_make_event(ttr=2.0, book_metrics=metrics))
    assert sig is None


def test_detect_skip_long_ttr():
    """TTR > 72h → no signal."""
    book = _make_book(bid_depth=15000, ask_depth=5000)
    metrics = compute_obi_metrics(book)
    sig = detect(_make_event(ttr=100.0, book_metrics=metrics))
    assert sig is None


def test_detect_skip_missing_book():
    """Нет book_metrics → no signal."""
    assert detect(_make_event(book_metrics=None)) is None


def test_detect_yes_side_uses_yes_token():
    """YES side берёт YES token_id из clob_token_ids[0].

    После NO-side filter (2026-05-22) D36 emit только YES signals,
    поэтому проверяем token resolution только для YES.
    """
    book = _make_book(bid_depth=15000, ask_depth=5000)
    metrics = compute_obi_metrics(book)
    sig = detect(_make_event(book_metrics=metrics))
    assert sig is not None
    assert sig.side == "YES"
    assert sig.token_id == "yes_token_xyz"


# --- Constants sanity ----------------------------------------------------


def test_constants_match_research():
    """Backtest constants должны соответствовать research/orthogonal-strategies/report.md."""
    assert MIN_OBI_ABS == 0.25
    assert MAX_SPREAD == 0.08
    assert MIN_TOTAL_DEPTH_USD == 5000.0


def test_polymarket_real_book_ordering():
    """Real Polymarket CLOB book ordering (discovered 2026-05-13):
    - bids ascending price (bids[0] = lowest = $0.01)
    - asks descending price (asks[0] = highest = $0.99)
    - Best bid = bids[-1], best ask = asks[-1]

    compute_obi_metrics должен корректно находить best bid/ask независимо от order.
    """
    real_book = {
        "bids": [
            {"price": "0.01", "size": "120000"},  # placeholder wall
            {"price": "0.05", "size": "24000"},
            {"price": "0.55", "size": "1000"},
            {"price": "0.58", "size": "70000"},
            {"price": "0.59", "size": "118000"},  # best bid
        ],
        "asks": [
            {"price": "0.99", "size": "105000"},  # placeholder wall
            {"price": "0.95", "size": "1300"},
            {"price": "0.63", "size": "112000"},
            {"price": "0.61", "size": "494000"},
            {"price": "0.60", "size": "352000"},  # best ask
        ],
    }
    m = compute_obi_metrics(real_book)
    assert m is not None
    # Best bid = 0.59, best ask = 0.60 → spread = 0.01
    assert abs(m["spread"] - 0.01) < 1e-6
    assert m["best_bid"] == 0.59
    assert m["best_ask"] == 0.60
    # spread filter должен пропустить (0.01 < 0.08)
    assert m["spread"] < MAX_SPREAD


def test_top5_depth_excludes_extreme_walls():
    """Depth считается только по top-5 levels, чтобы избежать distortion от
    extreme placeholder orders на 0.01 и 0.99.
    """
    book = {
        "bids": [
            {"price": "0.01", "size": "1000000"},  # огромная wall, не должна попасть в top5
            {"price": "0.40", "size": "100"},
            {"price": "0.45", "size": "100"},
            {"price": "0.50", "size": "100"},
            {"price": "0.55", "size": "100"},
            {"price": "0.58", "size": "100"},
        ],
        "asks": [
            {"price": "0.99", "size": "1000000"},  # огромная wall
            {"price": "0.95", "size": "100"},
            {"price": "0.80", "size": "100"},
            {"price": "0.70", "size": "100"},
            {"price": "0.65", "size": "100"},
            {"price": "0.60", "size": "100"},
        ],
    }
    m = compute_obi_metrics(book)
    assert m is not None
    # Top 5 bids: 0.40-0.58 (НЕ включая 0.01)
    # Top 5 asks: 0.60-0.95 (НЕ включая 0.99)
    # bid_depth = (0.40+0.45+0.50+0.55+0.58) × 100 = 248
    # ask_depth = (0.60+0.65+0.70+0.80+0.95) × 100 = 370
    assert abs(m["bid_depth_usd"] - 248.0) < 1.0
    assert abs(m["ask_depth_usd"] - 370.0) < 1.0
