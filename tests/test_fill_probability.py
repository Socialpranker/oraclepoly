"""Тесты stake-aware fill probability (PR1)."""

from __future__ import annotations

from app.services.virtual_execution import (
    FillProbability,
    estimate_maker_fill_probability,
)


def _book(asks: list[tuple[float, float]], bids: list[tuple[float, float]] | None = None) -> dict:
    """Удобный конструктор full book.
    asks: [(price, size), ...] sorted ascending."""
    return {
        "asks": [{"price": str(p), "size": str(s)} for p, s in asks],
        "bids": [{"price": str(p), "size": str(s)} for p, s in (bids or [])],
    }


def test_no_book_returns_zero():
    fp = estimate_maker_fill_probability(book=None, side="NO", limit_price=0.5, stake_usd=10.0)
    assert fp.fill_rate == 0.0
    assert fp.reason == "no_book"


def test_full_fill_when_depth_sufficient():
    """Limit 0.5, stake $10 → нужно купить 20 shares.
    Asks: 0.4@50shares — больше чем нужно → fill_rate=1.0."""
    book = _book(asks=[(0.4, 50.0)])
    fp = estimate_maker_fill_probability(book=book, side="NO", limit_price=0.5, stake_usd=10.0)
    assert fp.fill_rate == 1.0
    assert fp.book_depth_at_or_better > 0
    assert fp.reason == "full"


def test_partial_fill_when_depth_insufficient():
    """Limit 0.5, stake $10 → нужно 20 shares.
    Asks: 0.4@5 shares (только 5) → fill_rate = 5/20 = 0.25."""
    book = _book(asks=[(0.4, 5.0)])
    fp = estimate_maker_fill_probability(book=book, side="NO", limit_price=0.5, stake_usd=10.0)
    assert 0 < fp.fill_rate < 1.0
    assert fp.fill_rate == 0.25
    assert fp.reason == "partial"


def test_no_match_when_all_asks_above_limit():
    """Limit 0.4, asks: 0.5@100 — все asks выше limit → 0% fill."""
    book = _book(asks=[(0.5, 100.0)])
    fp = estimate_maker_fill_probability(book=book, side="NO", limit_price=0.4, stake_usd=10.0)
    assert fp.fill_rate == 0.0
    assert fp.reason == "no_match"


def test_multilevel_asks_aggregated():
    """Levels суммируются до limit:
    asks: 0.30@5, 0.35@5, 0.40@5, 0.50@100. Limit=0.40 → usable = 15 shares.
    Stake $10 → нужно 25 shares (10/0.4) → fill_rate = 15/25 = 0.6."""
    book = _book(asks=[(0.30, 5.0), (0.35, 5.0), (0.40, 5.0), (0.50, 100.0)])
    fp = estimate_maker_fill_probability(book=book, side="NO", limit_price=0.40, stake_usd=10.0)
    assert fp.fill_rate == 0.6
    assert fp.reason == "partial"


def test_book_top_size_extracted():
    book = _book(asks=[(0.4, 12.5), (0.5, 200.0)])
    fp = estimate_maker_fill_probability(book=book, side="NO", limit_price=0.5, stake_usd=10.0)
    assert fp.book_top_size == 12.5


def test_light_book_no_depth_data():
    """Light book {bid, ask} — нет depth → fallback на наличие price match."""
    light_book = {"bid": 0.4, "ask": 0.5}
    fp = estimate_maker_fill_probability(book=light_book, side="BUY", limit_price=0.45, stake_usd=10.0)
    assert fp.fill_rate == 0.0  # bid 0.4 < limit 0.45 → no match


def test_light_book_instant_fill_when_bid_at_or_above_limit():
    """Light book: best_bid >= limit → instant_top_only signal."""
    light_book = {"bid": 0.5, "ask": 0.6}
    fp = estimate_maker_fill_probability(book=light_book, side="BUY", limit_price=0.45, stake_usd=10.0)
    assert fp.fill_rate == 1.0
    assert fp.reason == "instant_top_only"


def test_zero_target_safe():
    """stake_usd=0 → 0 target shares → 0 fill (не упасть с DivByZero)."""
    book = _book(asks=[(0.4, 100.0)])
    fp = estimate_maker_fill_probability(book=book, side="NO", limit_price=0.5, stake_usd=0.0)
    assert fp.fill_rate == 0.0
    assert fp.reason == "zero_target"


def test_fillprobability_is_frozen_dataclass():
    fp = FillProbability(0.5, 100.0, 50.0)
    try:
        fp.fill_rate = 0.0  # type: ignore[misc]
        raise AssertionError("Should be frozen")
    except Exception:
        pass


def test_invalid_size_value_skipped():
    """Если size невалидно (TypeError) — level пропускается, остальные считаются."""
    book = {
        "asks": [{"price": "0.4", "size": "not_a_number"}, {"price": "0.4", "size": "10"}],
        "bids": [],
    }
    fp = estimate_maker_fill_probability(book=book, side="NO", limit_price=0.5, stake_usd=10.0)
    # Только второй level считается (10 shares × 0.4 = $4 depth)
    # Stake $10 / 0.5 = 20 shares target → 10/20 = 0.5
    assert fp.fill_rate == 0.5


def test_huge_stake_capped_at_full_book():
    """Если stake огромный (1M$) и book маленький — fill_rate = 0 (rounded)."""
    book = _book(asks=[(0.4, 5.0)])  # $2 USDC depth
    fp = estimate_maker_fill_probability(book=book, side="NO", limit_price=0.5, stake_usd=1_000_000.0)
    assert fp.fill_rate < 0.001
