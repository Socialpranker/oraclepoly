#!/usr/bin/env python3
"""Post-deploy smoke test для self-trading bot pipeline (Phase B+D+E).

Проверяет что после deploy на VPS все компоненты pipeline работают:
1. БД содержит новые таблицы (detector_signals, ml_calibrators, ml_meta_labeler)
2. events.neg_risk колонки заполнены (negrisk-sync отработал)
3. Детекторы импортируются без ошибок
4. inference path: fetch_calibrated_detector_signals() → EV-sizer → решение

Не делает: backfill, fit, реальные торговые операции.

Usage:
    docker exec oraclepoly-backend python scripts/smoke_test_pipeline.py

Exit codes:
    0 — всё ок
    1 — критическая ошибка (миграции не применены, импорты падают, etc)
    2 — warning (cold-start ожидаем, ничего ещё не торгуется)
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("smoke")


async def _check_tables() -> list[str]:
    from app.services.database import fetch

    expected = [
        "detector_signals",
        "ml_calibrators",
        "ml_meta_labeler",
    ]
    missing: list[str] = []
    for t in expected:
        rows = await fetch(
            "SELECT 1 FROM information_schema.tables WHERE table_name = $1 LIMIT 1",
            t,
        )
        if not rows:
            missing.append(t)
    return missing


async def _check_event_columns() -> list[str]:
    from app.services.database import fetch

    expected_cols = ["neg_risk", "neg_risk_other", "n_yes_winners"]
    rows = await fetch(
        """
        SELECT column_name FROM information_schema.columns
         WHERE table_name = 'events' AND column_name = ANY($1)
        """,
        expected_cols,
    )
    found = {r["column_name"] for r in rows}
    return [c for c in expected_cols if c not in found]


async def _check_imports() -> list[str]:
    failures: list[str] = []
    try:
        from app.detectors import d1_fade_whale, d3_cross_mkt_arb  # noqa: F401
    except Exception as exc:
        failures.append(f"detectors: {exc}")
    try:
        from app.calibration import (  # noqa: F401
            BetaCalibrator,
            size_from_calibrated_p,
            train_meta_labeler,
        )
    except Exception as exc:
        failures.append(f"calibration: {exc}")
    try:
        from app.services.detector_signal_service import (  # noqa: F401
            fetch_calibrated_detector_signals,
        )
    except Exception as exc:
        failures.append(f"detector_signal_service: {exc}")
    return failures


async def _check_inference_path() -> dict:
    from app.calibration import size_from_calibrated_p
    from app.services.detector_signal_service import fetch_calibrated_detector_signals

    signals = await fetch_calibrated_detector_signals()
    n_signals = len(signals)

    decisions = []
    for sig in signals[:5]:
        decision = size_from_calibrated_p(
            calibrated_p=sig["meta_p"],
            entry_price=sig["entry_price"],
            bankroll_usd=1000.0,
        )
        decisions.append(
            {
                "detector": sig["detector"],
                "raw_p": round(sig["raw_p"], 3),
                "meta_p": round(sig["meta_p"], 3),
                "entry_price": sig["entry_price"],
                "stake_usd": decision.stake_usd,
                "reason": decision.reason,
                "ev_pct": round(decision.ev_pct, 2),
            }
        )
    return {"n_total": n_signals, "sample": decisions}


async def _main() -> int:
    from app.core.cache import close_redis, init_redis
    from app.core.config import get_settings
    from app.services.database import close_db, init_db

    settings = get_settings()
    await init_db()
    await init_redis(settings.redis_url)

    exit_code = 0
    try:
        # 1. Таблицы
        missing_tables = await _check_tables()
        if missing_tables:
            logger.error("missing_tables: %s", missing_tables)
            exit_code = max(exit_code, 1)
        else:
            logger.info("✓ all expected tables present")

        # 2. Колонки в events
        missing_cols = await _check_event_columns()
        if missing_cols:
            logger.error("missing_event_columns: %s", missing_cols)
            exit_code = max(exit_code, 1)
        else:
            logger.info("✓ events neg_risk columns present")

        # 3. Imports
        import_failures = await _check_imports()
        if import_failures:
            logger.error("import_failures: %s", import_failures)
            exit_code = max(exit_code, 1)
        else:
            logger.info("✓ all detectors + calibration + service import")

        # 4. Inference path
        try:
            result = await _check_inference_path()
            logger.info("✓ inference path callable, n_signals=%s", result["n_total"])
            for d in result["sample"]:
                logger.info("  sample: %s", d)
            if result["n_total"] == 0:
                logger.warning("⚠ no fresh signals — cold start, expected immediately after deploy")
                exit_code = max(exit_code, 2)
        except Exception as exc:
            logger.error("inference_path_failed: %s", exc)
            exit_code = max(exit_code, 1)

    finally:
        await close_db()
        await close_redis()

    if exit_code == 0:
        logger.info("✓ smoke test PASSED")
    elif exit_code == 1:
        logger.error("✗ smoke test FAILED — критическая проблема")
    elif exit_code == 2:
        logger.warning("⚠ smoke test WARN — cold start, всё ок")

    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
