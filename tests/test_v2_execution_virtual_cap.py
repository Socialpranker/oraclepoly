"""V2 Execution — virtual per-detector exposure cap tests.

History:
  2026-05-03: cap = $1000 (initial user decision)
  2026-05-13: cap = $3000 (relaxed — STANDALONE_V2_LGB упиралось в $1K)
  2026-05-13: cap = $5000 (high-edge focus, allow more alpha capture)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.jobs import v2_execution


@pytest.mark.asyncio
async def test_virtual_exposure_remaining_zero_when_at_cap():
    """Cap = $5000, used = $5000 → remaining = $0."""
    with patch.object(v2_execution, "fetchval", new=AsyncMock(return_value=5000.0)):
        remaining = await v2_execution._virtual_exposure_remaining("D7b_no_favorite_fade")
    assert remaining == 0.0


@pytest.mark.asyncio
async def test_virtual_exposure_remaining_full_when_no_orders():
    """No open orders → remaining = full cap."""
    with patch.object(v2_execution, "fetchval", new=AsyncMock(return_value=0.0)):
        remaining = await v2_execution._virtual_exposure_remaining("D7b_no_favorite_fade")
    assert remaining == v2_execution.VIRTUAL_PER_DETECTOR_EXPOSURE_CAP_USD


@pytest.mark.asyncio
async def test_virtual_exposure_remaining_partial():
    """Used $300 → remaining = $4700 (cap $5000)."""
    with patch.object(v2_execution, "fetchval", new=AsyncMock(return_value=300.0)):
        remaining = await v2_execution._virtual_exposure_remaining("D3_cross_mkt_arb")
    assert remaining == 4700.0


@pytest.mark.asyncio
async def test_virtual_exposure_remaining_overshoot_clamped_to_zero():
    """If somehow used > cap (legacy data) → remaining clamped to 0, не negative.

    Используем detector БЕЗ override ($5K default). V2_LGB и D36 теперь имеют $10K
    override (selection bias audit 2026-05-19), их overshoot @ $7.5K не сработает.
    """
    with patch.object(v2_execution, "fetchval", new=AsyncMock(return_value=7500.0)):
        remaining = await v2_execution._virtual_exposure_remaining("D3_cross_mkt_arb")
    assert remaining == 0.0


@pytest.mark.asyncio
async def test_virtual_exposure_remaining_handles_none():
    """fetchval returns None если detector has no rows → treat as 0."""
    with patch.object(v2_execution, "fetchval", new=AsyncMock(return_value=None)):
        remaining = await v2_execution._virtual_exposure_remaining("D13_retail_confidence_fade")
    assert remaining == v2_execution.VIRTUAL_PER_DETECTOR_EXPOSURE_CAP_USD


def test_virtual_cap_constant_is_5000():
    """User-defined cap (2026-05-13 raise from $3K). Guard against accidental change."""
    assert v2_execution.VIRTUAL_PER_DETECTOR_EXPOSURE_CAP_USD == 5000.0


# ---------- Per-detector exposure cap overrides (2026-05-19 selection bias) ----------


def test_exposure_cap_override_v2_lgb_is_10k():
    """STANDALONE_V2_LGB: 1422 signals rejected by cap с WR=48.2% (winners loss).
    Override до $10K чтобы capture ~$13K/30d alpha."""
    assert v2_execution._exposure_cap_for("STANDALONE_V2_LGB") == 10000.0


def test_exposure_cap_override_d36_is_10k():
    """D36_obi_pressure: 86 signals rejected by cap с WR=52.3%. Override до $10K."""
    assert v2_execution._exposure_cap_for("D36_obi_pressure") == 10000.0


def test_exposure_cap_override_d38_is_15k():
    """D38_resolution_lag promoted 2026-05-24, Wilson lower 86.8% (n=430).
    Live rate 101 cap_rejects / 8 fills за час → override до $15K."""
    assert v2_execution._exposure_cap_for("D38_resolution_lag") == 15000.0


def test_exposure_cap_default_for_unknown_detector():
    """Detector без override → $5K default."""
    assert v2_execution._exposure_cap_for("D999_unknown") == 5000.0


def test_exposure_cap_default_for_other_detectors():
    """D5 rejected cap signals имели WR=17% (правильно blocked) → НЕ override, $5K."""
    assert v2_execution._exposure_cap_for("D5_long_horizon_extreme_fade") == 5000.0
    assert v2_execution._exposure_cap_for("D3_cross_mkt_arb") == 5000.0
    assert v2_execution._exposure_cap_for("D41_momentum") == 5000.0


@pytest.mark.asyncio
async def test_virtual_exposure_remaining_v2_lgb_uses_10k_cap():
    """V2_LGB с $4500 used → remaining $5500 (под $10K override), не $500 (под $5K default)."""
    with patch.object(v2_execution, "fetchval", new=AsyncMock(return_value=4500.0)):
        remaining = await v2_execution._virtual_exposure_remaining("STANDALONE_V2_LGB")
    assert remaining == 5500.0


@pytest.mark.asyncio
async def test_virtual_exposure_remaining_d36_uses_10k_cap():
    """D36 с $6000 used → remaining $4000 (под $10K override), не 0 (под $5K default)."""
    with patch.object(v2_execution, "fetchval", new=AsyncMock(return_value=6000.0)):
        remaining = await v2_execution._virtual_exposure_remaining("D36_obi_pressure")
    assert remaining == 4000.0
