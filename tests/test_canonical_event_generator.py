"""Canonical event generator — pure-logic unit tests.

Покрывают трансформацию Polymarket event → canonical поля без БД.
БД-оркестрация (run_canonical_event_generator) проверяется прогоном на проде.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.jobs.canonical_event_generator import (
    _coerce_resolves_at,
    build_canonical_slug,
    build_polymarket_url,
)
from app.matchers.polymarket_canonical_matcher import map_polymarket_category

# ── build_canonical_slug ────────────────────────────────────────────────


def test_slug_prefers_event_slug():
    assert build_canonical_slug("world-cup-winner", "12345") == "world-cup-winner"


def test_slug_strips_whitespace():
    assert build_canonical_slug("  fed-decision  ", "99") == "fed-decision"


def test_slug_fallback_to_polymarket_id_when_none():
    assert build_canonical_slug(None, "abc123") == "pm-event-abc123"


def test_slug_fallback_when_empty_string():
    assert build_canonical_slug("", "abc123") == "pm-event-abc123"


def test_slug_fallback_when_whitespace_only():
    assert build_canonical_slug("   ", "xyz") == "pm-event-xyz"


# ── build_polymarket_url ────────────────────────────────────────────────


def test_url_prefers_market_slug():
    assert (
        build_polymarket_url("0xcond", "btc-200k")
        == "https://polymarket.com/market/btc-200k"
    )


def test_url_fallback_to_condition_id():
    assert (
        build_polymarket_url("0xcond", None)
        == "https://polymarket.com/market/0xcond"
    )


def test_url_none_when_both_missing():
    assert build_polymarket_url(None, None) is None


def test_url_none_when_both_empty():
    assert build_polymarket_url("", "  ") is None


# ── _coerce_resolves_at ─────────────────────────────────────────────────


def test_coerce_passthrough_datetime():
    dt = datetime(2026, 7, 20, tzinfo=UTC)
    assert _coerce_resolves_at(dt) is dt


def test_coerce_none():
    assert _coerce_resolves_at(None) is None


def test_coerce_iso_string_with_z():
    result = _coerce_resolves_at("2026-03-19T19:00:00Z")
    assert result == datetime(2026, 3, 19, 19, 0, 0, tzinfo=UTC)


def test_coerce_invalid_string_returns_none():
    assert _coerce_resolves_at("not-a-date") is None


# ── category mapping (reused from matcher, sanity over real event cats) ──


def test_category_map_covers_real_event_categories():
    # Реальные категории из events на проде (verified 2026-06-05).
    assert map_polymarket_category("Crypto") == "crypto"
    assert map_polymarket_category("Politics") == "politics"
    assert map_polymarket_category("Sports") == "sports"
    assert map_polymarket_category("World & Economy") == "economics"
    assert map_polymarket_category("Tech & AI") == "macro"
    assert map_polymarket_category("Culture") == "macro"


def test_category_map_unknown_falls_back_to_macro():
    assert map_polymarket_category("Quantum Astrology") == "macro"
    assert map_polymarket_category("") == "macro"
