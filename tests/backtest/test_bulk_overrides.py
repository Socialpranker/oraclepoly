"""Tests для per-detector overrides в scripts/bulk_backtest.py.

Защита от регрессии: d14 ОБЯЗАН гнаться с --category crypto, иначе bulk на
широком окне ловит OOM (15.4M trade_bucket dict). См. инцидент 2026-06-14.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "bulk_backtest.py"


def _load_bulk_module():
    spec = importlib.util.spec_from_file_location("bulk_backtest_mod", _SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_d14_override_is_crypto():
    """d14 → category crypto (его домен; без этого OOM на all-cat окне)."""
    bulk = _load_bulk_module()
    assert "d14" in bulk._DETECTOR_OVERRIDES
    assert bulk._DETECTOR_OVERRIDES["d14"]["category"] == ["crypto"]


def test_overrides_only_known_detectors():
    """Карта overrides не должна содержать опечаток в именах детекторов."""
    from app.backtest.detector_adapter import _REGISTRY

    bulk = _load_bulk_module()
    for name in bulk._DETECTOR_OVERRIDES:
        assert name in _REGISTRY, f"override для незарегистрированного детектора: {name}"


def test_only_d14_requires_buckets():
    """Инвариант для bulk: buckets грузятся per-detector только требующим (d14).

    bulk использует `args.use_trade_buckets and requires_trade_buckets(name)`.
    Если bucket-требующих станет больше — каждый без category-фильтра рискует
    OOM на 15.4M строк, и для него тоже нужен override (как d14→crypto).
    """
    from app.backtest.detector_adapter import _REGISTRY, requires_trade_buckets

    bucket_detectors = {name for name in _REGISTRY if requires_trade_buckets(name)}
    assert bucket_detectors == {"d14"}, (
        f"bucket-требующие детекторы изменились: {bucket_detectors}. "
        "Каждому нужен category-override в _DETECTOR_OVERRIDES чтобы bulk не словил OOM."
    )
