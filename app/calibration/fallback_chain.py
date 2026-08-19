"""Единая calibration fallback chain: G18 bin → LGB → Beta → raw_p.

До этого рефакторинга цепочка жила в трёх копиях (detector_signal_service.py,
two_stage_inference.py — полные; model_drift_check.py — Beta-only) и минимум
один раз разошлась: прод-инцидент 2026-05-26, KeyError 'g18_bin' после PR #502.
Этот модуль — единственный источник правды для цепочки и загрузчиков.

Beta-only применение в model_drift_check.py — сознательно НЕ на полной цепочке:
drift-check измеряет качество самого Beta-калибратора.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from app.calibration.beta_calibrator import BetaCalibrator
from app.calibration.lgb_calibrator import LgbCalibrator
from app.calibration.lgb_features import build_features
from app.core.config import get_settings
from app.services.database import fetch

logger = structlog.get_logger()

# Все источники калибровки. Счётчики в call-sites инициализировать строго как
# dict.fromkeys(CAL_SOURCES, 0) — иначе повторится инцидент 2026-05-26
# (init счётчиков разошёлся с самой цепочкой).
CAL_SOURCES: tuple[str, ...] = ("g18_bin", "lgb", "beta", "raw")


async def load_l1_calibrators() -> dict[str, BetaCalibrator]:
    """Per-detector Beta-калибраторы из ml_calibrators (все строки, включая per-anchor ключи detector@{N}h)."""
    rows = await fetch("SELECT detector, params FROM ml_calibrators")
    out: dict[str, BetaCalibrator] = {}
    for r in rows:
        params = r["params"]
        if isinstance(params, str):
            params = json.loads(params)
        out[r["detector"]] = BetaCalibrator.deserialize(params)
    return out


async def load_lgb_calibrators() -> dict[str, LgbCalibrator]:
    """Per-detector LGB-калибраторы (только is_active=true).

    Primary calibrator в fallback chain LGB → Beta → raw_p. Cold-start safe:
    пустая или отсутствующая таблица → пустой dict, весь трафик идёт через Beta.
    """
    try:
        rows = await fetch("SELECT detector, model_payload FROM ml_calibrators_lgb WHERE is_active = true")
    except Exception as exc:
        # Таблица может ещё не существовать на dev/test окружениях.
        logger.info("lgb_calibrators_unavailable", error=str(exc))
        return {}
    out: dict[str, LgbCalibrator] = {}
    for r in rows:
        payload = r["model_payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        try:
            out[r["detector"]] = LgbCalibrator.deserialize(payload)
        except Exception as exc:
            logger.warning("lgb_deserialize_failed", detector=r["detector"], error=str(exc))
    return out


async def calibrate(
    raw_p: float,
    detector: str,
    cals: dict[str, BetaCalibrator],
    row: dict[str, Any] | None = None,
    lgb_cals: dict[str, LgbCalibrator] | None = None,
    anchor_hours: int | None = None,
    settings: Any = None,
) -> tuple[float, str]:
    """Calibration fallback chain: G18 bin → LGB → Beta → raw_p.

    Returns (calibrated_p, source), source ∈ CAL_SOURCES.

    anchor_hours: если задан И существует калибратор с ключом `detector@{anchor}h`,
    используется он (per-anchor STANDALONE). Иначе плоский `detector`-ключ.
    Сейчас НИ ОДИН call-site его не передаёт: fit-джобы (ml_calibrator_fit.py)
    per-anchor модели уже пишут, но подключение их к inference — отдельное
    решение (меняет live-поведение), см. план 2026-07-05-calibration-fallback-chain-dedup.

    settings: get_settings() вызывающего — прокидывать один раз на batch,
    а не на каждый сигнал в цикле. None → get_settings() внутри (back-compat).
    """
    if settings is None:
        settings = get_settings()
    cal_key = f"{detector}@{anchor_hours}h" if anchor_hours else detector

    # 0) G18 bin lookup (per-(detector × category × price_bin) historical WR),
    # только для whitelisted detectors под feature flag. См. app/services/g18_bin_calibration.py.
    #
    # current_price ОБЯЗАН быть реальной ценой рынка, а не raw_p/confidence: у детекторов
    # с константным confidence (напр. D38 всегда 0.95) fallback на raw_p подставлял бы
    # константу-вероятность в price_bin lookup вместо реальной цены → бин всегда один и тот
    # же, независимо от фактической цены рынка. Нет current_price → tier пропускается.
    if settings.calibration_bins_enabled and row is not None and row.get("current_price") is not None:
        # Lazy import: g18_bin_calibration живёт в app/services и тянет свой стек.
        from app.services.g18_bin_calibration import bin_lookup

        category = row.get("category")
        current_price = float(row["current_price"])
        try:
            p_g18, src_g18 = await bin_lookup(detector, category, current_price)
            if p_g18 is not None and 0.0 < p_g18 < 1.0:
                return float(p_g18), src_g18
        except Exception as exc:
            logger.warning("g18_lookup_failed", detector=detector, error=str(exc))
    elif settings.calibration_bins_enabled and row is not None:
        logger.debug("g18_no_data", detector=detector, reason="missing_current_price")

    # 1) LGB calibrator (если активен). Per-anchor ключ → fallback на плоский detector.
    if lgb_cals is not None and row is not None:
        lgb = lgb_cals.get(cal_key) or lgb_cals.get(detector)
        if lgb is not None and lgb.params is not None:
            try:
                feats = build_features(row)
                p = float(lgb.transform(feats))
                if 0.0 < p < 1.0:
                    return p, "lgb"
            except Exception as exc:
                logger.warning("lgb_transform_failed", detector=detector, error=str(exc))

    # 2) Beta calibrator. Per-anchor ключ → fallback на плоский detector.
    cal = cals.get(cal_key) or cals.get(detector)
    if cal is None or cal.params is None:
        return raw_p, "raw"
    try:
        return float(cal.transform(raw_p)), "beta"
    except Exception as exc:
        logger.warning("l1_transform_failed", detector=detector, error=str(exc))
        return raw_p, "raw"
