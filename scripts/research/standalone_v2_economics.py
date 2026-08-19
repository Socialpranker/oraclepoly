#!/usr/bin/env python3
"""Economics simulation для standalone V2 — посчитать сколько денег заработал бы.

Вход: все резолвнутые predictions из standalone_v2_predictions.
Стратегия:
  - Если |EV| > MIN_EV_PCT и market_yes_price OK → bet
  - Side: YES если ev_yes_pct > ev_no_pct, иначе NO
  - Stake: fractional Kelly, capped at MAX_STAKE
  - Slippage 1%, fees 2% (Polymarket realistic)

Output: PnL с разными порогами EV.
"""

from __future__ import annotations

import asyncio
import os

import asyncpg


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


async def main() -> None:
    conn = await asyncpg.connect(_get_db_dsn())
    try:
        for anchor_h in [24, 72, 168]:
            print(f"\n{'='*70}")
            print(f"ANCHOR={anchor_h}h")
            print(f"{'='*70}")

            for min_ev_pct, label in [(5, "EV>5%"), (10, "EV>10%"), (20, "EV>20%"), (30, "EV>30%")]:
                rows = await conn.fetch(f"""
                    SELECT
                        condition_id,
                        event_id,
                        predicted_p_yes,
                        market_yes_price,
                        ev_yes_pct,
                        ev_no_pct,
                        yes_won
                    FROM standalone_v2_predictions
                    WHERE anchor_hours = {anchor_h}
                      AND yes_won IS NOT NULL
                      AND market_yes_price IS NOT NULL
                      AND market_yes_price BETWEEN 0.05 AND 0.95
                      AND (ABS(ev_yes_pct) > {min_ev_pct} OR ABS(ev_no_pct) > {min_ev_pct})
                """)

                if not rows:
                    continue

                stake_per_trade = 5.0  # $5 per trade (warmup level)
                slippage_pct = 1.0
                fee_pct = 2.0
                total_pnl = 0.0
                n_yes = 0
                n_no = 0
                n_wins = 0
                event_pnls: dict[str, float] = {}

                for r in rows:
                    yes_p = float(r["market_yes_price"])
                    ev_yes = float(r["ev_yes_pct"]) if r["ev_yes_pct"] else 0
                    ev_no = float(r["ev_no_pct"]) if r["ev_no_pct"] else 0
                    won = int(r["yes_won"])

                    # Choose side
                    if abs(ev_yes) > abs(ev_no) and ev_yes > min_ev_pct:
                        entry = yes_p * (1 + slippage_pct/100)
                        n_yes += 1
                        if won == 1:
                            payout = stake_per_trade / entry
                            pnl = payout - stake_per_trade - stake_per_trade * fee_pct/100
                            n_wins += 1
                        else:
                            pnl = -stake_per_trade
                    elif ev_no > min_ev_pct:
                        no_p = 1 - yes_p
                        entry = no_p * (1 + slippage_pct/100)
                        n_no += 1
                        if won == 0:
                            payout = stake_per_trade / entry
                            pnl = payout - stake_per_trade - stake_per_trade * fee_pct/100
                            n_wins += 1
                        else:
                            pnl = -stake_per_trade
                    else:
                        continue

                    total_pnl += pnl
                    if r["event_id"]:
                        event_pnls[r["event_id"]] = event_pnls.get(r["event_id"], 0) + pnl

                n_trades = n_yes + n_no
                if n_trades == 0:
                    continue
                wr = 100 * n_wins / n_trades
                roi = 100 * total_pnl / (n_trades * stake_per_trade)
                # Per-event aggregation
                n_events = len(event_pnls)
                event_wins = sum(1 for v in event_pnls.values() if v > 0)
                event_wr = 100 * event_wins / n_events if n_events else 0

                print(f"\n  {label}: n_trades={n_trades:,} (YES={n_yes}, NO={n_no})  WR={wr:.1f}%")
                print(f"    PnL=${total_pnl:+,.0f}  ROI={roi:+.1f}%  Stake=${stake_per_trade}/trade")
                print(f"    n_events={n_events:,}  event_WR={event_wr:.1f}%")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
