#!/usr/bin/env python3
"""standalone_v2_dry_runner — paper trading для standalone V2 model.

Каждый час (или backfill):
1. Берёт unresolved standalone_v2_predictions с |EV| > MIN_EV_PCT
2. Создаёт dry trade (entry_price + slippage, default $5 stake) → INSERT в standalone_v2_dry_trades
3. Backfill outcomes для уже-резолвнутых dry trades (gross PnL, net PnL после fees)

Параметры (из спецификации экономики):
  MIN_EV_PCT = 10.0           # порог входа (sweet spot)
  STAKE_USD_DEFAULT = 5.0     # дефолтный stake
  STAKE_OVERRIDE                # per-(category, anchor_hours) bigger stake для scale-up test
  SLIPPAGE_PCT = 1.0          # market impact
  FEE_PCT = 2.0               # exchange fee + gas
  MAX_PRICE_RANGE = (0.05, 0.95)  # avoid тонкие края

Запуск:
  /root/oracle/.venv-research/bin/python /root/oracle/scripts/research/standalone_v2_dry_runner.py
  /root/oracle/.venv-research/bin/python /root/oracle/scripts/research/standalone_v2_dry_runner.py --backfill
"""

from __future__ import annotations

import argparse
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

# Trading parameters
MIN_EV_PCT = 10.0
STAKE_USD_DEFAULT = 5.0
SLIPPAGE_PCT = 1.0
FEE_PCT = 2.0
PRICE_MIN = 0.05
PRICE_MAX = 0.95

# Scale-up test 2026-05-02: per-(category, anchor_hours) stake override.
# Top-3 buckets by 7d real PnL. Linear scale × 4 → expect 4× pnl/day if Polymarket book absorbs $20.
# If sub-linear (slippage съедает margin) — это даст true ceiling. Revert to $5 after 2-3 days.
STAKE_OVERRIDE: dict[tuple[str, int], float] = {
    ("Other", 24): 20.0,
    ("Other", 72): 20.0,
    ("Geopolitics", 24): 20.0,
}


def _resolve_stake(category: str | None, anchor_hours: int) -> float:
    if category and (category, anchor_hours) in STAKE_OVERRIDE:
        return STAKE_OVERRIDE[(category, anchor_hours)]
    return STAKE_USD_DEFAULT


async def create_dry_trades(conn: asyncpg.Connection, only_unresolved: bool = False) -> int:
    """Создаём dry trades для predictions которых ещё нет в dry_trades."""
    where_resolved = "AND p.yes_won IS NULL" if only_unresolved else ""
    rows = await conn.fetch(f"""
        SELECT
            p.id AS prediction_id, p.market_id, p.condition_id, p.event_id,
            p.anchor_hours, p.predicted_p_yes, p.market_yes_price,
            p.ev_yes_pct, p.ev_no_pct, p.category, p.horizon_days,
            p.model_version, p.yes_won
        FROM standalone_v2_predictions p
        LEFT JOIN standalone_v2_dry_trades dt ON dt.prediction_id = p.id
        WHERE dt.id IS NULL  -- ещё нет dry trade
          AND p.market_yes_price IS NOT NULL
          AND p.market_yes_price BETWEEN {PRICE_MIN} AND {PRICE_MAX}
          AND (
              ABS(COALESCE(p.ev_yes_pct, 0)) > {MIN_EV_PCT}
              OR ABS(COALESCE(p.ev_no_pct, 0)) > {MIN_EV_PCT}
          )
          {where_resolved}
        LIMIT 200000
    """)

    insert_rows = []
    for r in rows:
        ev_yes = float(r["ev_yes_pct"]) if r["ev_yes_pct"] is not None else 0.0
        ev_no = float(r["ev_no_pct"]) if r["ev_no_pct"] is not None else 0.0
        yes_p = float(r["market_yes_price"])
        won = r["yes_won"]

        # Choose side
        if ev_yes > MIN_EV_PCT and ev_yes >= ev_no:
            side = "YES"
            entry = yes_p * (1 + SLIPPAGE_PCT / 100)
            ev_pct = ev_yes
        elif ev_no > MIN_EV_PCT:
            side = "NO"
            entry = (1 - yes_p) * (1 + SLIPPAGE_PCT / 100)
            ev_pct = ev_no
        else:
            continue

        if entry <= 0 or entry >= 1:
            continue

        stake_usd = _resolve_stake(r["category"], int(r["anchor_hours"]))
        expected_payout = stake_usd / entry  # only при WIN
        fees = stake_usd * (FEE_PCT / 100) + (expected_payout - stake_usd) * (FEE_PCT / 100)

        # Compute outcome if уже резолвнут
        if won is not None:
            is_winner = (side == "YES" and won == 1) or (side == "NO" and won == 0)
            if is_winner:
                pnl_gross = expected_payout - stake_usd
            else:
                pnl_gross = -stake_usd
            pnl_net = pnl_gross - (fees if is_winner else stake_usd * FEE_PCT / 100)
            actual_fees = fees if is_winner else stake_usd * FEE_PCT / 100
        else:
            is_winner = None
            pnl_gross = None
            pnl_net = None
            actual_fees = None

        insert_rows.append((
            r["prediction_id"], r["market_id"], r["condition_id"], r["event_id"],
            r["anchor_hours"], side,
            float(r["predicted_p_yes"]), yes_p, ev_pct, entry, stake_usd, expected_payout,
            won, is_winner, pnl_gross, pnl_net, actual_fees,
            r["category"], r["horizon_days"], r["model_version"],
        ))

    if not insert_rows:
        logger.info("no_new_trades_to_create")
        return 0

    await conn.executemany("""
        INSERT INTO standalone_v2_dry_trades (
            prediction_id, market_id, condition_id, event_id, anchor_hours, side,
            predicted_p_yes, market_yes_price, ev_pct, entry_price, stake_usd, expected_payout,
            yes_won, is_winner, pnl_gross, pnl_net, fees_usd,
            category, horizon_days, model_version
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20)
        ON CONFLICT (prediction_id) DO NOTHING
    """, insert_rows)

    # Set resolved_at for resolved ones
    await conn.execute("""
        UPDATE standalone_v2_dry_trades
        SET resolved_at = NOW()
        WHERE yes_won IS NOT NULL AND resolved_at IS NULL
    """)

    return len(insert_rows)


async def backfill_outcomes(conn: asyncpg.Connection) -> int:
    """Backfill outcomes for unresolved dry trades если prediction уже резолвнут."""
    n = await conn.execute(f"""
        UPDATE standalone_v2_dry_trades dt
        SET yes_won = p.yes_won,
            is_winner = (
                (dt.side = 'YES' AND p.yes_won = 1)
                OR (dt.side = 'NO' AND p.yes_won = 0)
            ),
            pnl_gross = CASE
                WHEN (dt.side = 'YES' AND p.yes_won = 1)
                  OR (dt.side = 'NO' AND p.yes_won = 0)
                  THEN dt.expected_payout - dt.stake_usd
                ELSE -dt.stake_usd
            END,
            pnl_net = CASE
                WHEN (dt.side = 'YES' AND p.yes_won = 1)
                  OR (dt.side = 'NO' AND p.yes_won = 0)
                  THEN (dt.expected_payout - dt.stake_usd) - (dt.expected_payout - dt.stake_usd) * {FEE_PCT}/100 - dt.stake_usd * {FEE_PCT}/100
                ELSE -dt.stake_usd - dt.stake_usd * {FEE_PCT}/100
            END,
            fees_usd = CASE
                WHEN (dt.side = 'YES' AND p.yes_won = 1)
                  OR (dt.side = 'NO' AND p.yes_won = 0)
                  THEN (dt.expected_payout - dt.stake_usd) * {FEE_PCT}/100 + dt.stake_usd * {FEE_PCT}/100
                ELSE dt.stake_usd * {FEE_PCT}/100
            END,
            resolved_at = NOW()
        FROM standalone_v2_predictions p
        WHERE dt.prediction_id = p.id
          AND dt.yes_won IS NULL
          AND p.yes_won IS NOT NULL
    """)
    return int(n.split()[-1]) if n else 0


async def report_summary(conn: asyncpg.Connection) -> None:
    rows = await conn.fetch("""
        SELECT
            anchor_hours,
            COUNT(*)::int AS n_trades,
            SUM(CASE WHEN is_winner THEN 1 ELSE 0 END)::int AS n_wins,
            COUNT(CASE WHEN yes_won IS NOT NULL THEN 1 END)::int AS n_resolved,
            ROUND(SUM(pnl_net)::numeric, 2)::float AS total_pnl,
            ROUND(SUM(stake_usd)::numeric, 2)::float AS total_stake
        FROM standalone_v2_dry_trades
        GROUP BY anchor_hours ORDER BY anchor_hours
    """)
    logger.info("=== DRY TRADES SUMMARY ===")
    for r in rows:
        wr = (100 * r["n_wins"] / r["n_resolved"]) if r["n_resolved"] else 0
        roi = (100 * r["total_pnl"] / r["total_stake"]) if r["total_stake"] else 0
        logger.info(
            "  anchor=%dh: trades=%d resolved=%d WR=%.1f%% PnL=$%+.0f ROI=%+.1f%%",
            r["anchor_hours"], r["n_trades"], r["n_resolved"], wr,
            (r["total_pnl"] or 0), roi,
        )


async def main_async(backfill: bool) -> None:
    conn = await asyncpg.connect(_get_db_dsn())
    try:
        n_new = await create_dry_trades(conn, only_unresolved=not backfill)
        logger.info("created_dry_trades=%d", n_new)

        n_back = await backfill_outcomes(conn)
        logger.info("backfilled_outcomes=%d", n_back)

        await report_summary(conn)
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", action="store_true",
                        help="Process all eligible predictions (incl. resolved). Default: only unresolved.")
    args = parser.parse_args()
    asyncio.run(main_async(args.backfill))


if __name__ == "__main__":
    main()
