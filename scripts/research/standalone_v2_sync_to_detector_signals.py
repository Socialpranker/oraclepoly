#!/usr/bin/env python3
"""standalone_v2_sync_to_detector_signals — incremental sync.

После того как cron'ы (predictor + dry_runner) добавили новые predictions/dry_trades,
этот скрипт sync'ит их в detector_signals и execution_orders чтобы они появились в
стандартных 4 tabs (/v2/detectors, BacktestTerminal/StatusTerminal/VirtualTerminal).

Идемпотентен через NOT EXISTS на (detector, market_id, generated_at).

Запуск (cron каждый час :45, после :00 predictor и :30 dry_runner):
  /root/oracle/.venv-research/bin/python /root/oracle/scripts/research/standalone_v2_sync_to_detector_signals.py
"""

from __future__ import annotations

import asyncio
import logging
import os

import asyncpg

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

def _get_db_dsn() -> str:
    """Resolve DB DSN from env at connect time (fail-fast, no hardcoded secret)."""
    try:
        return os.environ["DB_DSN"]
    except KeyError as exc:
        raise SystemExit(
            "DB_DSN env var must be set "
            "(in prod cron it is exported as $DATABASE_URL inside the container; "
            "for manual runs export DB_DSN before invoking this script)"
        ) from exc


SYNC_DETECTOR_SIGNALS_SQL = """
INSERT INTO detector_signals (
    detector, market_id, event_id, side,
    confidence, expected_edge_pct, fair_price, current_price,
    horizon_sec, evidence, generated_at,
    resolved, outcome, realized_roi, realized_roi_net, resolved_at,
    source
)
SELECT
    'STANDALONE_V2_LGB',
    p.condition_id, p.event_id,
    CASE WHEN COALESCE(p.ev_yes_pct, 0) >= COALESCE(p.ev_no_pct, 0) THEN 'YES' ELSE 'NO' END,
    CASE WHEN COALESCE(p.ev_yes_pct, 0) >= COALESCE(p.ev_no_pct, 0) THEN p.predicted_p_yes ELSE 1.0 - p.predicted_p_yes END,
    GREATEST(COALESCE(p.ev_yes_pct, 0), COALESCE(p.ev_no_pct, 0)),
    p.predicted_p_yes, p.market_yes_price,
    p.anchor_hours * 3600,
    jsonb_build_object('anchor_hours', p.anchor_hours, 'model_version', p.model_version,
                       'predicted_p_yes', p.predicted_p_yes, 'ev_yes_pct', p.ev_yes_pct,
                       'ev_no_pct', p.ev_no_pct, 'category', p.category),
    p.inference_at,
    p.yes_won IS NOT NULL,
    CASE
        WHEN p.yes_won IS NULL THEN NULL
        WHEN ((CASE WHEN COALESCE(p.ev_yes_pct, 0) >= COALESCE(p.ev_no_pct, 0) THEN 'YES' ELSE 'NO' END) = 'YES' AND p.yes_won = 1)
          OR ((CASE WHEN COALESCE(p.ev_yes_pct, 0) >= COALESCE(p.ev_no_pct, 0) THEN 'YES' ELSE 'NO' END) = 'NO' AND p.yes_won = 0) THEN 'WIN'
        ELSE 'LOSS'
    END,
    CASE
        WHEN p.yes_won IS NULL OR p.market_yes_price IS NULL THEN NULL
        WHEN (CASE WHEN COALESCE(p.ev_yes_pct, 0) >= COALESCE(p.ev_no_pct, 0) THEN 'YES' ELSE 'NO' END) = 'YES' AND p.yes_won = 1 AND p.market_yes_price > 0
            THEN ROUND(((1.0 / p.market_yes_price - 1.0) * 100)::numeric, 3)
        WHEN (CASE WHEN COALESCE(p.ev_yes_pct, 0) >= COALESCE(p.ev_no_pct, 0) THEN 'YES' ELSE 'NO' END) = 'NO' AND p.yes_won = 0 AND p.market_yes_price < 1
            THEN ROUND(((1.0 / (1.0 - p.market_yes_price) - 1.0) * 100)::numeric, 3)
        ELSE -100.0
    END,
    CASE
        WHEN p.yes_won IS NULL OR p.market_yes_price IS NULL THEN NULL
        WHEN (CASE WHEN COALESCE(p.ev_yes_pct, 0) >= COALESCE(p.ev_no_pct, 0) THEN 'YES' ELSE 'NO' END) = 'YES' AND p.yes_won = 1 AND p.market_yes_price > 0
            THEN ROUND((((1.0 / (p.market_yes_price * 1.01) - 1.0) * 100) - 4.0)::numeric, 3)
        WHEN (CASE WHEN COALESCE(p.ev_yes_pct, 0) >= COALESCE(p.ev_no_pct, 0) THEN 'YES' ELSE 'NO' END) = 'NO' AND p.yes_won = 0 AND p.market_yes_price < 1
            THEN ROUND((((1.0 / ((1.0 - p.market_yes_price) * 1.01) - 1.0) * 100) - 4.0)::numeric, 3)
        ELSE -102.0
    END,
    p.resolved_at,
    -- Source tag: legacy backfill (один большой INSERT 2026-05-02) vs live cron emissions.
    -- live_v2 means: predictor запустился на свежих live_ml_features → активные markets,
    -- эти signals попадают в virtual execution и становятся реальными paper позициями.
    -- hf_backfill = одноразовый исторический backfill, не trading.
    CASE WHEN p.inference_at >= TIMESTAMP '2026-05-05 00:00:00' THEN 'live_v2' ELSE 'hf_backfill' END
FROM standalone_v2_predictions p
WHERE p.anchor_hours = 24
  AND (COALESCE(p.ev_yes_pct, 0) > 10 OR COALESCE(p.ev_no_pct, 0) > 10)
  AND p.market_yes_price BETWEEN 0.05 AND 0.95
  AND NOT EXISTS (
      SELECT 1 FROM detector_signals ds
      WHERE ds.detector = 'STANDALONE_V2_LGB'
        AND ds.market_id = p.condition_id
        AND ds.generated_at = p.inference_at
  )
"""

# 72h sync — ТОЛЬКО для трейна per-anchor калибраторов (см. plan per-anchor calibration).
# КРИТИЧНО: только resolved (yes_won IS NOT NULL) → resolved=true ВСЕГДА. Execution-fetcher
# (fetch_signals_for_active_pipeline → WHERE resolved=false) их НИКОГДА не возьмёт → 72h
# не торгуется. execution_orders для 72h НЕ создаётся (SYNC_EXECUTION_ORDERS_SQL = 24h only).
# Структура зеркалит 24h-INSERT (тот же ev>10 фильтр для консистентности калибраторов).
SYNC_DETECTOR_SIGNALS_72H_TRAIN_SQL = """
INSERT INTO detector_signals (
    detector, market_id, event_id, side,
    confidence, expected_edge_pct, fair_price, current_price,
    horizon_sec, evidence, generated_at,
    resolved, outcome, realized_roi, realized_roi_net, resolved_at,
    source
)
SELECT
    'STANDALONE_V2_LGB',
    p.condition_id, p.event_id,
    CASE WHEN COALESCE(p.ev_yes_pct, 0) >= COALESCE(p.ev_no_pct, 0) THEN 'YES' ELSE 'NO' END,
    CASE WHEN COALESCE(p.ev_yes_pct, 0) >= COALESCE(p.ev_no_pct, 0) THEN p.predicted_p_yes ELSE 1.0 - p.predicted_p_yes END,
    GREATEST(COALESCE(p.ev_yes_pct, 0), COALESCE(p.ev_no_pct, 0)),
    p.predicted_p_yes, p.market_yes_price,
    p.anchor_hours * 3600,
    jsonb_build_object('anchor_hours', p.anchor_hours, 'model_version', p.model_version,
                       'predicted_p_yes', p.predicted_p_yes, 'ev_yes_pct', p.ev_yes_pct,
                       'ev_no_pct', p.ev_no_pct, 'category', p.category),
    p.inference_at,
    true,  -- только resolved синкаем (см. guard ниже) → resolved всегда true
    CASE
        WHEN ((CASE WHEN COALESCE(p.ev_yes_pct, 0) >= COALESCE(p.ev_no_pct, 0) THEN 'YES' ELSE 'NO' END) = 'YES' AND p.yes_won = 1)
          OR ((CASE WHEN COALESCE(p.ev_yes_pct, 0) >= COALESCE(p.ev_no_pct, 0) THEN 'YES' ELSE 'NO' END) = 'NO' AND p.yes_won = 0) THEN 'WIN'
        ELSE 'LOSS'
    END,
    CASE
        WHEN p.market_yes_price IS NULL THEN NULL
        WHEN (CASE WHEN COALESCE(p.ev_yes_pct, 0) >= COALESCE(p.ev_no_pct, 0) THEN 'YES' ELSE 'NO' END) = 'YES' AND p.yes_won = 1 AND p.market_yes_price > 0
            THEN ROUND(((1.0 / p.market_yes_price - 1.0) * 100)::numeric, 3)
        WHEN (CASE WHEN COALESCE(p.ev_yes_pct, 0) >= COALESCE(p.ev_no_pct, 0) THEN 'YES' ELSE 'NO' END) = 'NO' AND p.yes_won = 0 AND p.market_yes_price < 1
            THEN ROUND(((1.0 / (1.0 - p.market_yes_price) - 1.0) * 100)::numeric, 3)
        ELSE -100.0
    END,
    CASE
        WHEN p.market_yes_price IS NULL THEN NULL
        WHEN (CASE WHEN COALESCE(p.ev_yes_pct, 0) >= COALESCE(p.ev_no_pct, 0) THEN 'YES' ELSE 'NO' END) = 'YES' AND p.yes_won = 1 AND p.market_yes_price > 0
            THEN ROUND((((1.0 / (p.market_yes_price * 1.01) - 1.0) * 100) - 4.0)::numeric, 3)
        WHEN (CASE WHEN COALESCE(p.ev_yes_pct, 0) >= COALESCE(p.ev_no_pct, 0) THEN 'YES' ELSE 'NO' END) = 'NO' AND p.yes_won = 0 AND p.market_yes_price < 1
            THEN ROUND((((1.0 / ((1.0 - p.market_yes_price) * 1.01) - 1.0) * 100) - 4.0)::numeric, 3)
        ELSE -102.0
    END,
    p.resolved_at,
    CASE WHEN p.inference_at >= TIMESTAMP '2026-05-05 00:00:00' THEN 'live_v2' ELSE 'hf_backfill' END
FROM standalone_v2_predictions p
WHERE p.anchor_hours = 72
  AND p.yes_won IS NOT NULL  -- ТОЛЬКО resolved → resolved=true → не попадёт в execution
  AND (COALESCE(p.ev_yes_pct, 0) > 10 OR COALESCE(p.ev_no_pct, 0) > 10)
  AND p.market_yes_price BETWEEN 0.05 AND 0.95
  AND NOT EXISTS (
      SELECT 1 FROM detector_signals ds
      WHERE ds.detector = 'STANDALONE_V2_LGB'
        AND ds.market_id = p.condition_id
        AND ds.generated_at = p.inference_at
  )
"""

SYNC_EXECUTION_ORDERS_SQL = """
INSERT INTO execution_orders (
    market_id, event_id, side, execution_mode,
    signal_price, order_type, stake, fill_price, filled_qty,
    fee_paid, status, pnl, outcome,
    created_at, placed_at, filled_at, settled_at,
    detector, calibrated_p, meta_p, ev_pct,
    trading_fee_usd, slippage_usd, net_pnl, meta
)
SELECT
    dt.condition_id, dt.event_id, dt.side, 'virtual',
    dt.market_yes_price, 'limit', dt.stake_usd, dt.entry_price,
    ROUND((dt.stake_usd / NULLIF(dt.entry_price, 0))::numeric, 2),
    COALESCE(dt.fees_usd, 0),
    CASE WHEN dt.yes_won IS NOT NULL THEN 'virtual_settled' ELSE 'virtual_placed' END,
    dt.pnl_gross,
    CASE WHEN dt.is_winner IS NULL THEN NULL WHEN dt.is_winner THEN 'WIN' ELSE 'LOSS' END,
    dt.decision_at, dt.decision_at, dt.decision_at, dt.resolved_at,
    'STANDALONE_V2_LGB', dt.predicted_p_yes, dt.predicted_p_yes, dt.ev_pct,
    COALESCE(dt.fees_usd, 0),
    ROUND((ABS(dt.entry_price - CASE WHEN dt.side='YES' THEN dt.market_yes_price ELSE 1.0 - dt.market_yes_price END) * dt.stake_usd / NULLIF(dt.entry_price, 0))::numeric, 4),
    dt.pnl_net,
    jsonb_build_object(
        'source',
        CASE WHEN dt.decision_at >= TIMESTAMP '2026-05-05 00:00:00' THEN 'live_v2' ELSE 'hf_backfill' END,
        'anchor_hours', dt.anchor_hours,
        'model_version', dt.model_version, 'predicted_p_yes', dt.predicted_p_yes,
        'category', dt.category, 'horizon_days', dt.horizon_days
    )
FROM standalone_v2_dry_trades dt
WHERE dt.anchor_hours = 24
  AND NOT EXISTS (
      SELECT 1 FROM execution_orders eo
      WHERE eo.execution_mode = 'virtual'
        AND eo.detector = 'STANDALONE_V2_LGB'
        AND eo.market_id = dt.condition_id
        AND eo.created_at = dt.decision_at
  )
"""


async def main_async() -> None:
    conn = await asyncpg.connect(_get_db_dsn())
    try:
        n_signals = await conn.execute(SYNC_DETECTOR_SIGNALS_SQL)
        logger.info("synced_detector_signals=%s", n_signals.split()[-1] if n_signals else 0)

        # 72h resolved-only — для трейна per-anchor калибраторов (не торгуется, см. SQL comment).
        n_72h = await conn.execute(SYNC_DETECTOR_SIGNALS_72H_TRAIN_SQL)
        logger.info("synced_detector_signals_72h_train=%s", n_72h.split()[-1] if n_72h else 0)

        n_orders = await conn.execute(SYNC_EXECUTION_ORDERS_SQL)
        logger.info("synced_execution_orders=%s", n_orders.split()[-1] if n_orders else 0)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main_async())
