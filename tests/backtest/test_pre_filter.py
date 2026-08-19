"""Unit tests для PreFilter — mirror prod SQL pre-filters."""

from __future__ import annotations

from app.backtest.pre_filter import PreFilter, get_pre_filter


def _event(**overrides) -> dict:
    """Default market_event passing all defaults."""
    base = {
        "current_price": 0.5,
        "hours_to_resolve": 100.0,
        "category": "Sports",
        "volume_24h": 10000.0,
        "neg_risk": False,
    }
    base.update(overrides)
    return base


# === current_price ===


def test_pre_filter_price_range_inclusive_min_exclusive_max() -> None:
    """SQL: >= min AND < max — boundary cases."""
    pf = PreFilter(current_price_min=0.85, current_price_max=0.97)
    assert pf.passes(_event(current_price=0.85))  # boundary low — inclusive
    assert pf.passes(_event(current_price=0.96))
    assert not pf.passes(_event(current_price=0.97))  # boundary high — exclusive
    assert not pf.passes(_event(current_price=0.84))
    assert not pf.passes(_event(current_price=0.98))


# === hours_to_resolve ===


def test_pre_filter_dte_range_inclusive_min_exclusive_max() -> None:
    pf = PreFilter(hours_to_resolve_min=4.0, hours_to_resolve_max=72.0)
    assert pf.passes(_event(hours_to_resolve=4.0))
    assert pf.passes(_event(hours_to_resolve=71.9))
    assert not pf.passes(_event(hours_to_resolve=72.0))
    assert not pf.passes(_event(hours_to_resolve=3.9))


# === categories ===


def test_pre_filter_include_whitelist() -> None:
    pf = PreFilter(categories_include=frozenset({"Sports", "Other"}))
    assert pf.passes(_event(category="Sports"))
    assert pf.passes(_event(category="Other"))
    assert not pf.passes(_event(category="Crypto"))
    assert not pf.passes(_event(category=""))


def test_pre_filter_exclude_blacklist() -> None:
    pf = PreFilter(categories_exclude=frozenset({"Culture"}))
    assert pf.passes(_event(category="Sports"))
    assert not pf.passes(_event(category="Culture"))


def test_pre_filter_include_and_exclude_both_apply() -> None:
    """Если include задан AND exclude задан — оба применяются."""
    pf = PreFilter(
        categories_include=frozenset({"Sports", "Culture"}),
        categories_exclude=frozenset({"Culture"}),
    )
    assert pf.passes(_event(category="Sports"))
    assert not pf.passes(_event(category="Culture"))
    assert not pf.passes(_event(category="Other"))


# === volume_24h ===


def test_pre_filter_volume_min_only() -> None:
    """D7b style: volume_24h >= 5000."""
    pf = PreFilter(volume_24h_min=5000.0)
    assert pf.passes(_event(volume_24h=5000.0))
    assert pf.passes(_event(volume_24h=100000.0))
    assert not pf.passes(_event(volume_24h=4999.0))


def test_pre_filter_volume_between_inclusive_both() -> None:
    """D31/D32/D33 style: BETWEEN 100 AND 5000 — оба boundary inclusive."""
    pf = PreFilter(volume_24h_min=100.0, volume_24h_max=5000.0)
    assert pf.passes(_event(volume_24h=100.0))
    assert pf.passes(_event(volume_24h=5000.0))
    assert pf.passes(_event(volume_24h=2500.0))
    assert not pf.passes(_event(volume_24h=99.0))
    assert not pf.passes(_event(volume_24h=5001.0))


def test_pre_filter_volume_none_defaults_to_zero() -> None:
    """volume_24h=None → treated as 0 (mirror COALESCE(volume_24h, 0))."""
    pf = PreFilter(volume_24h_min=5000.0)
    assert not pf.passes(_event(volume_24h=None))


# === neg_risk ===


def test_pre_filter_neg_risk_required_false() -> None:
    """D33 style: neg_risk = false OR null."""
    pf = PreFilter(neg_risk_required=False)
    assert pf.passes(_event(neg_risk=False))
    assert pf.passes(_event(neg_risk=None))  # None → bool(None)=False → match
    assert not pf.passes(_event(neg_risk=True))


def test_pre_filter_neg_risk_required_none_no_constraint() -> None:
    """neg_risk_required=None — не ограничивает."""
    pf = PreFilter(neg_risk_required=None)
    assert pf.passes(_event(neg_risk=True))
    assert pf.passes(_event(neg_risk=False))


# === Registry lookup ===


def test_get_pre_filter_known_detector() -> None:
    """D7b registered with prod SQL mirror."""
    pf = get_pre_filter("d7b")
    assert pf.current_price_min == 0.85
    assert pf.current_price_max == 0.97
    assert pf.volume_24h_min == 5000.0
    assert "Culture" in pf.categories_exclude


def test_get_pre_filter_unknown_detector_noop() -> None:
    """Unknown detector → no-op PreFilter (всё проходит)."""
    pf = get_pre_filter("d_does_not_exist")
    # Default — пустые ranges = all pass
    assert pf.passes(_event())
    assert pf.passes(_event(current_price=0.01))
    assert pf.passes(_event(current_price=0.99))


def test_get_pre_filter_case_insensitive() -> None:
    """Lookup is lowercase-normalized."""
    pf_lower = get_pre_filter("d7b")
    pf_upper = get_pre_filter("D7B")
    assert pf_lower == pf_upper


# === D7b-specific integration ===


def test_d7b_filter_mirrors_prod_scan() -> None:
    """D7b SQL: price ∈ [0.85, 0.97), DTE ∈ [4, 72)h, NOT Culture, vol_24h >= 5000."""
    pf = get_pre_filter("d7b")

    # Passing: typical D7b candidate
    assert pf.passes(_event(
        current_price=0.90,
        hours_to_resolve=24,
        category="Sports",
        volume_24h=10000,
    ))

    # Fails: Culture excluded
    assert not pf.passes(_event(
        current_price=0.90,
        hours_to_resolve=24,
        category="Culture",
        volume_24h=10000,
    ))

    # Fails: volume too low (prod scan filters out)
    assert not pf.passes(_event(
        current_price=0.90,
        hours_to_resolve=24,
        category="Sports",
        volume_24h=4000,
    ))

    # Fails: price too high
    assert not pf.passes(_event(
        current_price=0.97,
        hours_to_resolve=24,
        category="Sports",
        volume_24h=10000,
    ))


def test_d33_filter_neg_risk_required() -> None:
    """D33 requires neg_risk=false."""
    pf = get_pre_filter("d33")
    base = _event(
        current_price=0.85,
        hours_to_resolve=72,
        category="Politics",
        volume_24h=500,
    )
    assert pf.passes({**base, "neg_risk": False})
    assert pf.passes({**base, "neg_risk": None})
    assert not pf.passes({**base, "neg_risk": True})


def test_d44_filter_is_noop() -> None:
    """D44 has empty PreFilter — detector сам делает все checks."""
    pf = get_pre_filter("d44")
    # Любой market_event проходит — D44 detector сам отфильтрует
    assert pf.passes(_event())
    assert pf.passes(_event(current_price=0.01, volume_24h=0))
