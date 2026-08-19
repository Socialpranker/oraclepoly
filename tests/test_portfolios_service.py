"""Phase 2 of portfolios refactor — service layer tests.

Тестируем pure functions (apply_gate_overrides) + DB-mocked fetch helpers.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services import portfolios as portfolios_svc
from app.services.portfolios import (
    Portfolio,
    apply_gate_overrides,
    fetch_all_portfolios,
    fetch_portfolio,
    fetch_portfolio_for_detector,
    fetch_portfolio_map,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Сбрасываем TTL cache между тестами."""
    portfolios_svc.invalidate_cache()
    yield
    portfolios_svc.invalidate_cache()


# ─── apply_gate_overrides (pure) ─────────────────────────────────────────────


def test_apply_overrides_skip_marks_passed():
    gates = {
        "adversarial_mirror": {"passed": False, "value": "n/a"},
        "pnl_positive": {"passed": True, "value": 10.0},
    }
    new_gates, applied = apply_gate_overrides(gates, {"adversarial_mirror": "skip"})
    assert new_gates["adversarial_mirror"]["passed"] is True
    assert new_gates["adversarial_mirror"]["override"] == "skip"
    # untouched
    assert new_gates["pnl_positive"]["passed"] is True
    assert "override" not in new_gates["pnl_positive"]
    assert applied == ["adversarial_mirror"]


def test_apply_overrides_unknown_key_noop():
    gates = {"pnl_positive": {"passed": True, "value": 10.0}}
    new_gates, applied = apply_gate_overrides(gates, {"nonexistent_gate": "skip"})
    assert new_gates == gates
    assert applied == []


def test_apply_overrides_unknown_action_noop():
    gates = {"pnl_positive": {"passed": False, "value": -5.0}}
    new_gates, applied = apply_gate_overrides(gates, {"pnl_positive": "delete"})
    # Unknown action → no change
    assert new_gates["pnl_positive"]["passed"] is False
    assert "override" not in new_gates["pnl_positive"]
    assert applied == []


def test_apply_overrides_does_not_mutate_input():
    gates = {"adversarial_mirror": {"passed": False, "value": "n/a"}}
    overrides = {"adversarial_mirror": "skip"}
    apply_gate_overrides(gates, overrides)
    # Input untouched
    assert gates["adversarial_mirror"]["passed"] is False
    assert "override" not in gates["adversarial_mirror"]


def test_apply_overrides_empty_overrides_returns_copy():
    gates = {"pnl_positive": {"passed": True}}
    new_gates, applied = apply_gate_overrides(gates, {})
    assert new_gates == gates
    assert new_gates is not gates  # deep copy
    assert applied == []


def test_apply_overrides_relax_marks_annotation_only():
    gates = {"days_accumulated": {"passed": False, "value": 2, "threshold": 14}}
    new_gates, applied = apply_gate_overrides(gates, {"days_accumulated": "relax"})
    # relax — caller-handled threshold, не auto-passes gate
    assert new_gates["days_accumulated"]["passed"] is False
    assert new_gates["days_accumulated"]["override"] == "relax"
    assert applied == ["days_accumulated"]


def test_apply_overrides_multiple_gates():
    gates = {
        "adversarial_mirror": {"passed": False},
        "canonical_verdict": {"passed": False},
        "pnl_positive": {"passed": True},
    }
    new_gates, applied = apply_gate_overrides(
        gates, {"adversarial_mirror": "skip", "canonical_verdict": "skip"}
    )
    assert new_gates["adversarial_mirror"]["passed"] is True
    assert new_gates["canonical_verdict"]["passed"] is True
    assert new_gates["pnl_positive"]["passed"] is True
    assert sorted(applied) == ["adversarial_mirror", "canonical_verdict"]


# ─── Portfolio dataclass + row mapping ───────────────────────────────────────


def test_portfolio_dataclass_frozen():
    p = Portfolio(
        portfolio_id="rules",
        display_name="Rules",
        bankroll_usd=3000.0,
        exposure_cap_usd=3000.0,
        kelly_fraction=0.20,
    )
    with pytest.raises(Exception):  # FrozenInstanceError
        p.bankroll_usd = 5000.0  # type: ignore[misc]


def test_portfolio_kelly_fraction_within_bounds():
    """Sanity: kelly должен быть в [0, 1]."""
    p = Portfolio(
        portfolio_id="ml",
        display_name="ML",
        bankroll_usd=1000.0,
        exposure_cap_usd=1000.0,
        kelly_fraction=0.10,
    )
    assert 0.0 <= p.kelly_fraction <= 1.0


# ─── DB-mocked fetch helpers ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_portfolio_returns_dataclass():
    mock_row = {
        "portfolio_id": "rules",
        "display_name": "Rules-based",
        "bankroll_usd": 3000.00,
        "exposure_cap_usd": 3000.00,
        "kelly_fraction": 0.200,
        "gate_overrides": {},
        "enabled": True,
    }
    with patch("app.services.portfolios.fetchrow", new=AsyncMock(return_value=mock_row)):
        p = await fetch_portfolio("rules")
    assert p is not None
    assert p.portfolio_id == "rules"
    assert p.bankroll_usd == 3000.0
    assert p.kelly_fraction == 0.2
    assert p.gate_overrides == {}


@pytest.mark.asyncio
async def test_fetch_portfolio_returns_none_when_missing():
    with patch("app.services.portfolios.fetchrow", new=AsyncMock(return_value=None)):
        p = await fetch_portfolio("nonexistent")
    assert p is None


@pytest.mark.asyncio
async def test_fetch_portfolio_parses_jsonb_overrides_from_string():
    """Если asyncpg вернул gate_overrides как JSON string — парсим явно."""
    mock_row = {
        "portfolio_id": "ml",
        "display_name": "ML",
        "bankroll_usd": 1000.00,
        "exposure_cap_usd": 1000.00,
        "kelly_fraction": 0.100,
        "gate_overrides": '{"adversarial_mirror": "skip"}',
        "enabled": True,
    }
    with patch("app.services.portfolios.fetchrow", new=AsyncMock(return_value=mock_row)):
        p = await fetch_portfolio("ml")
    assert p is not None
    assert p.gate_overrides == {"adversarial_mirror": "skip"}


@pytest.mark.asyncio
async def test_fetch_portfolio_for_detector_with_mock_db():
    mock_row = {
        "portfolio_id": "ml",
        "display_name": "ML model",
        "bankroll_usd": 1000.00,
        "exposure_cap_usd": 1000.00,
        "kelly_fraction": 0.100,
        "gate_overrides": {"adversarial_mirror": "skip"},
        "enabled": True,
    }
    with patch("app.services.portfolios.fetchrow", new=AsyncMock(return_value=mock_row)):
        p = await fetch_portfolio_for_detector("STANDALONE_V2_LGB")
    assert p is not None
    assert p.portfolio_id == "ml"
    assert p.gate_overrides == {"adversarial_mirror": "skip"}


@pytest.mark.asyncio
async def test_fetch_all_portfolios_caches_within_ttl():
    rows = [
        {
            "portfolio_id": "rules",
            "display_name": "Rules",
            "bankroll_usd": 3000.00,
            "exposure_cap_usd": 3000.00,
            "kelly_fraction": 0.200,
            "gate_overrides": {},
            "enabled": True,
        },
    ]
    mock_fetch = AsyncMock(return_value=rows)
    with patch("app.services.portfolios.fetch", new=mock_fetch):
        await fetch_all_portfolios()
        await fetch_all_portfolios()
        await fetch_all_portfolios()
    # Three calls, only one DB hit (cache)
    assert mock_fetch.call_count == 1


@pytest.mark.asyncio
async def test_fetch_all_portfolios_returns_dataclasses():
    rows = [
        {
            "portfolio_id": "rules",
            "display_name": "Rules",
            "bankroll_usd": 3000.00,
            "exposure_cap_usd": 3000.00,
            "kelly_fraction": 0.200,
            "gate_overrides": {},
            "enabled": True,
        },
        {
            "portfolio_id": "ml",
            "display_name": "ML",
            "bankroll_usd": 1000.00,
            "exposure_cap_usd": 1000.00,
            "kelly_fraction": 0.100,
            "gate_overrides": {"adversarial_mirror": "skip"},
            "enabled": True,
        },
    ]
    with patch("app.services.portfolios.fetch", new=AsyncMock(return_value=rows)):
        out = await fetch_all_portfolios()
    assert len(out) == 2
    assert all(isinstance(p, Portfolio) for p in out)
    assert out[0].portfolio_id == "rules"
    assert out[1].portfolio_id == "ml"


@pytest.mark.asyncio
async def test_fetch_portfolio_map_returns_detector_to_pid():
    rows = [
        {"detector": "D7b_no_favorite_fade", "portfolio_id": "rules"},
        {"detector": "STANDALONE_V2_LGB", "portfolio_id": "ml"},
    ]
    with patch("app.services.portfolios.fetch", new=AsyncMock(return_value=rows)):
        result = await fetch_portfolio_map()
    assert result == {
        "D7b_no_favorite_fade": "rules",
        "STANDALONE_V2_LGB": "ml",
    }


@pytest.mark.asyncio
async def test_fetch_portfolio_for_detector_caches_per_detector():
    mock_row = {
        "portfolio_id": "rules",
        "display_name": "Rules",
        "bankroll_usd": 3000.00,
        "exposure_cap_usd": 3000.00,
        "kelly_fraction": 0.200,
        "gate_overrides": {},
        "enabled": True,
    }
    mock_fetchrow = AsyncMock(return_value=mock_row)
    with patch("app.services.portfolios.fetchrow", new=mock_fetchrow):
        await fetch_portfolio_for_detector("D7b_no_favorite_fade")
        await fetch_portfolio_for_detector("D7b_no_favorite_fade")
    assert mock_fetchrow.call_count == 1
