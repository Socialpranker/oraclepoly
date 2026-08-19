"""Тесты per-anchor калибровки STANDALONE_V2_LGB.

Покрывает:
- _calibrator_key: composite-ключ detector@{anchor}h для per-anchor детекторов, плоский для прочих.
- _calibrate: lookup по composite-ключу с graceful fallback на плоский detector.
- Backward-compat: anchor_hours=None ведёт себя ровно как раньше (прочие детекторы не задеты).
"""

from __future__ import annotations

import pytest

from app.calibration.beta_calibrator import BetaCalibrator, BetaParams
from app.jobs.ml_calibrator_fit import _calibrator_key as beta_key
from app.jobs.ml_calibrator_lgb_fit import _calibrator_key as lgb_key
from app.services.detector_signal_service import _calibrate

# ── _calibrator_key (обе копии должны вести себя идентично) ───────────────────


@pytest.mark.parametrize("key_fn", [beta_key, lgb_key])
def test_key_standalone_per_anchor(key_fn):
    assert key_fn("STANDALONE_V2_LGB", "24") == "STANDALONE_V2_LGB@24h"
    assert key_fn("STANDALONE_V2_LGB", "72") == "STANDALONE_V2_LGB@72h"


@pytest.mark.parametrize("key_fn", [beta_key, lgb_key])
def test_key_other_detectors_flat(key_fn):
    # Не-per-anchor детекторы — плоский ключ независимо от anchor.
    assert key_fn("D36_obi_pressure", "24") == "D36_obi_pressure"
    assert key_fn("D38_resolution_lag", "72") == "D38_resolution_lag"


@pytest.mark.parametrize("key_fn", [beta_key, lgb_key])
def test_key_no_anchor_falls_back_flat(key_fn):
    # Даже для STANDALONE: без anchor → плоский ключ (graceful).
    assert key_fn("STANDALONE_V2_LGB", None) == "STANDALONE_V2_LGB"
    assert key_fn("STANDALONE_V2_LGB", "") == "STANDALONE_V2_LGB"


# ── _calibrate composite-ключ + fallback ─────────────────────────────────────


def _fitted_beta(constant: float) -> BetaCalibrator:
    """Beta-калибратор, всегда возвращающий ~constant (degenerate constant_q путь)."""
    cal = BetaCalibrator()
    cal.params = BetaParams(a=0.0, b=0.0, c=0.0, degenerate=True, constant_q=constant)
    return cal


@pytest.mark.asyncio
async def test_calibrate_uses_anchor_key_when_present():
    # Есть и @24h, и плоский — должен выбрать @24h.
    cals = {
        "STANDALONE_V2_LGB@24h": _fitted_beta(0.10),
        "STANDALONE_V2_LGB": _fitted_beta(0.90),
    }
    p, src = await _calibrate(0.5, "STANDALONE_V2_LGB", cals, anchor_hours=24)
    assert src == "beta"
    assert abs(p - 0.10) < 1e-6  # взял @24h, не плоский


@pytest.mark.asyncio
async def test_calibrate_falls_back_to_flat_when_anchor_key_missing():
    # Нет @72h, есть плоский — fallback на плоский.
    cals = {"STANDALONE_V2_LGB": _fitted_beta(0.33)}
    p, src = await _calibrate(0.5, "STANDALONE_V2_LGB", cals, anchor_hours=72)
    assert src == "beta"
    assert abs(p - 0.33) < 1e-6


@pytest.mark.asyncio
async def test_calibrate_anchor_none_is_unchanged_behavior():
    # Backward-compat: anchor_hours=None (все прочие call-sites) → плоский ключ.
    cals = {"D36_obi_pressure": _fitted_beta(0.42)}
    p, src = await _calibrate(0.5, "D36_obi_pressure", cals)
    assert src == "beta"
    assert abs(p - 0.42) < 1e-6


@pytest.mark.asyncio
async def test_calibrate_no_calibrator_returns_raw():
    # Нет ни composite, ни плоского → raw passthrough.
    p, src = await _calibrate(0.7, "STANDALONE_V2_LGB", {}, anchor_hours=24)
    assert src == "raw"
    assert p == 0.7
