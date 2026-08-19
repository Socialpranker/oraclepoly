"""Tests: STANDALONE_V2 predictor applies calibration to p_yes before EV/side.

Root cause (2026-05-29): predictor used raw model.predict_proba for p_yes →
ev_yes/ev_no → side, with no calibration. Raw LGB over-predicts p_yes (51% says
vs 24% actual) → YES side -5.4%. Fix: apply _calibrate chain before EV/side.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# Predictor lives in scripts/research/ — load it as a module by path.
_PRED_PATH = Path(__file__).resolve().parents[1] / "scripts" / "research" / "standalone_v2_predictor.py"
_spec = importlib.util.spec_from_file_location("standalone_v2_predictor", _PRED_PATH)
predictor = importlib.util.module_from_spec(_spec)
sys.modules["standalone_v2_predictor"] = predictor
_spec.loader.exec_module(predictor)


# ─── decide_side_ev: pure EV + side decision ─────────────────────────────────


def test_decide_side_ev_picks_yes_when_p_above_price():
    """p_yes > yes_price → YES has positive edge → side YES."""
    side, ev_yes, ev_no = predictor.decide_side_ev(p_yes=0.70, yes_price=0.50)
    assert side == "YES"
    assert ev_yes > 0
    assert ev_yes >= ev_no


def test_decide_side_ev_flips_to_no_when_calibration_lowers_p():
    """Raw p_yes=0.60 would pick YES; calibrated down to 0.40 → flips to NO.

    This is the core fix: side decided on calibrated p, not raw.
    """
    raw_side, _, _ = predictor.decide_side_ev(p_yes=0.60, yes_price=0.50)
    cal_side, _, _ = predictor.decide_side_ev(p_yes=0.40, yes_price=0.50)
    assert raw_side == "YES"
    assert cal_side == "NO"


# ─── predict_all: calibration wired into the pipeline ────────────────────────


class _StubModel:
    """Fake LGB — returns fixed P(yes)=raw for every row."""

    def __init__(self, raw: float):
        self._raw = raw

    def predict_proba(self, X):
        import numpy as np

        n = len(X)
        return np.column_stack([np.full(n, 1 - self._raw), np.full(n, self._raw)])


class _HalvingBeta:
    """Fake calibrator: halves raw_p (simulates correcting overshoot)."""

    params = object()  # non-None so _calibrate uses it

    def transform(self, raw_p):
        return float(raw_p) / 2.0


def _one_row_df(raw_p: float, yes_price: float):
    import pandas as pd

    return pd.DataFrame(
        [{
            "anchor_hours": 24,
            "category": "Politics",
            "feat_a": 1.0,
            "yes_price_at_anchor": yes_price,
            "horizon_days": 2.0,
            "condition_id": "0xabc",
            "event_id": "evt1",
        }]
    )


_MODELS = {24: {"model": None, "feat_cols": ["feat_a", "category"], "version": "test_v2"}}


@pytest.mark.asyncio
async def test_predict_all_writes_calibrated_p_not_raw():
    """С калибратором, понижающим p, в INSERT-кортеж идёт calibrated_p, не raw."""
    models = {24: {**_MODELS[24], "model": _StubModel(0.80)}}
    df = _one_row_df(raw_p=0.80, yes_price=0.45)  # не 0.5 (placeholder-skip guard)
    rows = await predictor.predict_all(df, models, cals={"STANDALONE_V2_LGB": _HalvingBeta()}, lgb_cals={})
    assert len(rows) == 1
    predicted_p_yes = rows[0][5]  # 6th column в INSERT tuple
    assert predicted_p_yes == pytest.approx(0.40)  # 0.80 raw → 0.40 calibrated


@pytest.mark.asyncio
async def test_predict_all_cold_start_passes_raw_through():
    """Без калибраторов (cold-start) → predicted_p_yes = raw (passthrough, safe)."""
    models = {24: {**_MODELS[24], "model": _StubModel(0.80)}}
    df = _one_row_df(raw_p=0.80, yes_price=0.45)
    rows = await predictor.predict_all(df, models, cals={}, lgb_cals={})
    assert len(rows) == 1
    assert rows[0][5] == pytest.approx(0.80)
