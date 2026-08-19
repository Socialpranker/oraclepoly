"""backtest_metadata parser — unit tests."""

from __future__ import annotations

from app.services.backtest_metadata import (
    list_available_backtests,
    parse_backtest_md,
)


def test_parse_d10b_returns_metadata():
    """D10b markdown: frontmatter + body parsed correctly."""
    data = parse_backtest_md("D10b_election_ladder")
    assert data is not None
    assert data["detector"] == "D10b_election_ladder"
    assert data["verdict"] == "PASS"
    assert isinstance(data["n_signals"], int)
    assert data["n_signals"] > 0
    assert isinstance(data["honest_haircut_factor"], float)
    assert "body_markdown" in data
    assert len(data["body_markdown"]) > 100


def test_parse_missing_returns_none():
    """Несуществующий детектор → None."""
    assert parse_backtest_md("D99_nonexistent") is None


def test_list_available_includes_d10b():
    detectors = list_available_backtests()
    assert "D10b_election_ladder" in detectors


def test_parser_coerces_types():
    """Bool/int/float/str detection."""
    d = parse_backtest_md("D10b_election_ladder")
    assert d is not None
    assert isinstance(d["n_signals"], int)
    assert isinstance(d["honest_haircut_factor"], float)
    assert isinstance(d["verdict"], str)
