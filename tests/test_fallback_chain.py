"""Тесты единой calibration fallback chain (app/calibration/fallback_chain.py).

Цепочка: G18 bin → LGB → Beta → raw_p. Стабы duck-type'ят Beta/LGB-калибраторы
(модулю нужны только .params и .transform) — реальные params-схемы не нужны.
"""

import asyncio
import json
import random
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.calibration.beta_calibrator import BetaCalibrator
from app.calibration.fallback_chain import CAL_SOURCES, calibrate, load_l1_calibrators, load_lgb_calibrators

_G18_OFF = SimpleNamespace(calibration_bins_enabled=False)


class _StubCal:
    def __init__(self, out: float = 0.62, fail: bool = False, params_none: bool = False):
        self.params = None if params_none else {"stub": True}
        self._out = out
        self._fail = fail

    def transform(self, _x):
        if self._fail:
            raise ValueError("boom")
        return self._out


def _fitted_beta_calibrator() -> BetaCalibrator:
    """Реально обученный Beta-калибратор на синтетике (для round-trip serialize/deserialize)."""
    rng = random.Random(0)
    raw_p = [rng.uniform(0.3, 0.8) for _ in range(50)]
    outcome = [1 if rng.random() < 0.6 else 0 for _ in range(50)]
    cal = BetaCalibrator(min_samples=30)
    cal.fit(raw_p, outcome)
    return cal


def test_cal_sources_covers_all_chain_tiers():
    assert set(CAL_SOURCES) == {"g18_bin", "lgb", "beta", "raw"}


def test_raw_when_no_calibrators():
    p, src = asyncio.run(calibrate(0.7, "D1", {}, settings=_G18_OFF))
    assert (p, src) == (0.7, "raw")


def test_beta_applied():
    p, src = asyncio.run(calibrate(0.7, "D1", {"D1": _StubCal(out=0.62)}, settings=_G18_OFF))
    assert (p, src) == (0.62, "beta")


def test_beta_transform_failure_falls_back_to_raw():
    p, src = asyncio.run(calibrate(0.7, "D1", {"D1": _StubCal(fail=True)}, settings=_G18_OFF))
    assert (p, src) == (0.7, "raw")


def test_lgb_preferred_over_beta():
    with patch("app.calibration.fallback_chain.build_features", return_value=[0.0]):
        p, src = asyncio.run(
            calibrate(
                0.7, "D1", {"D1": _StubCal(out=0.62)},
                row={"raw_p": 0.7}, lgb_cals={"D1": _StubCal(out=0.55)}, settings=_G18_OFF,
            )
        )
    assert (p, src) == (0.55, "lgb")


def test_lgb_failure_falls_back_to_beta():
    with patch("app.calibration.fallback_chain.build_features", return_value=[0.0]):
        p, src = asyncio.run(
            calibrate(
                0.7, "D1", {"D1": _StubCal(out=0.62)},
                row={"raw_p": 0.7}, lgb_cals={"D1": _StubCal(fail=True)}, settings=_G18_OFF,
            )
        )
    assert (p, src) == (0.62, "beta")


def test_per_anchor_key_preferred_with_flat_fallback():
    cals = {"D1": _StubCal(out=0.62), "D1@24h": _StubCal(out=0.71)}
    p, src = asyncio.run(calibrate(0.7, "D1", cals, anchor_hours=24, settings=_G18_OFF))
    assert (p, src) == (0.71, "beta")
    # anchor задан, но per-anchor ключа нет → плоский detector-ключ
    p, src = asyncio.run(calibrate(0.7, "D1", cals, anchor_hours=72, settings=_G18_OFF))
    assert (p, src) == (0.62, "beta")


def test_g18_wins_when_enabled():
    g18_on = SimpleNamespace(calibration_bins_enabled=True)
    with patch(
        "app.services.g18_bin_calibration.bin_lookup",
        new=AsyncMock(return_value=(0.8, "g18_bin")),
    ):
        p, src = asyncio.run(
            calibrate(0.7, "D1", {}, row={"category": "Sports", "current_price": 0.4}, settings=g18_on)
        )
    assert (p, src) == (0.8, "g18_bin")


def test_load_lgb_calibrators_swallows_db_errors():
    # Таблицы может не быть на dev/test — loader обязан вернуть {} без падения.
    with patch(
        "app.calibration.fallback_chain.fetch",
        new=AsyncMock(side_effect=RuntimeError("no table")),
    ):
        assert asyncio.run(load_lgb_calibrators()) == {}


def test_lgb_skipped_when_row_is_none():
    # lgb_cals задан, но row=None → LGB-tier пропускается по guard'у, build_features не вызывается → Beta.
    with patch("app.calibration.fallback_chain.build_features") as mock_bf:
        p, src = asyncio.run(
            calibrate(0.7, "D1", {"D1": _StubCal(out=0.62)}, row=None, lgb_cals={"D1": _StubCal(out=0.55)}, settings=_G18_OFF)
        )
    mock_bf.assert_not_called()
    assert (p, src) == (0.62, "beta")


def test_lgb_skipped_when_params_none():
    # LGB-запись существует, но params=None (не обучен/деактивирован) → пропуск LGB → Beta.
    with patch("app.calibration.fallback_chain.build_features", return_value=[0.0]):
        p, src = asyncio.run(
            calibrate(
                0.7, "D1", {"D1": _StubCal(out=0.62)},
                row={"raw_p": 0.7}, lgb_cals={"D1": _StubCal(out=0.55, params_none=True)}, settings=_G18_OFF,
            )
        )
    assert (p, src) == (0.62, "beta")


def test_beta_params_none_falls_back_to_raw():
    # Beta-калибратор существует в dict, но params=None (не обучен) → raw_p.
    p, src = asyncio.run(calibrate(0.7, "D1", {"D1": _StubCal(params_none=True)}, settings=_G18_OFF))
    assert (p, src) == (0.7, "raw")


def test_g18_exception_falls_through_to_beta():
    # G18 включён, но bin_lookup падает — цепочка должна продолжить, а не пробросить исключение.
    g18_on = SimpleNamespace(calibration_bins_enabled=True)
    with patch(
        "app.services.g18_bin_calibration.bin_lookup",
        new=AsyncMock(side_effect=RuntimeError("g18 down")),
    ):
        p, src = asyncio.run(
            calibrate(
                0.7, "D1", {"D1": _StubCal(out=0.62)},
                row={"category": "Sports", "current_price": 0.4}, settings=g18_on,
            )
        )
    assert (p, src) == (0.62, "beta")


def test_g18_skipped_when_current_price_missing():
    # row без current_price (напр. детектор с константным confidence, напр. D38 ~0.95)
    # НЕ должен подставлять raw_p/confidence вместо цены в price_bin lookup — tier
    # пропускается целиком, bin_lookup не вызывается, цепочка идёт дальше (Beta).
    g18_on = SimpleNamespace(calibration_bins_enabled=True)
    with patch(
        "app.services.g18_bin_calibration.bin_lookup",
        new=AsyncMock(return_value=(0.8, "g18_bin")),
    ) as mock_lookup:
        p, src = asyncio.run(
            calibrate(
                0.95, "D38", {"D38": _StubCal(out=0.62)},
                row={"category": "Sports"}, settings=g18_on,
            )
        )
    mock_lookup.assert_not_called()
    assert (p, src) == (0.62, "beta")


def test_load_l1_calibrators_deserializes_rows():
    cal = _fitted_beta_calibrator()
    rows = [{"detector": "D1", "params": json.dumps(cal.serialize())}]
    with patch("app.calibration.fallback_chain.fetch", new=AsyncMock(return_value=rows)):
        out = asyncio.run(load_l1_calibrators())
    assert set(out) == {"D1"}
    assert hasattr(out["D1"], "transform")
    q = out["D1"].transform(0.7)
    assert 0.0 < q < 1.0
