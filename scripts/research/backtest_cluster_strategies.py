"""Бэктест cluster стратегий с Kelly + daily limit (как в проде).

Запуск: python scripts/research/backtest_cluster_strategies.py [--days N]
"""

import asyncio
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv

load_dotenv()

from app.core.cluster_classifier import Tier, classify_snapshot  # noqa: E402
from app.core.kelly_sizing import calculate_stake  # noqa: E402
from app.services.database import close_db, init_db  # noqa: E402

# Фильтры: (snap, cluster_result) -> bool
STRATEGIES: dict[str, dict] = {
    "sniper": {
        "filter": lambda snap, cr: cr.tier == Tier.T1,
        "desc": "T1 clusters only (80%+ verified)",
    },
    "timing_edge": {
        "filter": lambda snap, cr: cr.tier <= Tier.T2,
        "desc": "T1+T2 clusters (70%+)",
    },
    "contra_sniper": {
        "filter": lambda snap, cr: (
            float(snap.get("whale_contribution_raw") or 0) < -0.1
            and int(snap.get("whale_tx_count") or 0) > 0
        ),
        "desc": "Whale contra = oracle confirmation (85-100%)",
    },
    "clean_oracle": {
        "filter": lambda snap, cr: (
            int(snap.get("whale_tx_count") or 0) == 0
            and float(snap.get("whale_contribution_raw") or 0) == 0
        ),
        "desc": "No whale activity = purest oracle signal (91%)",
    },
}

INITIAL_BANKROLL = 1000.0
DAILY_LIMIT = 500.0
ENTRY_PRICE_FLOOR = 0.15


async def main():
    days = 7
    if "--days" in sys.argv:
        idx = sys.argv.index("--days")
        if idx + 1 < len(sys.argv):
            days = int(sys.argv[idx + 1])

    await init_db()

    from app.services.database import fetch

    rows = await fetch(
        """
        SELECT os.market_id, os.oracle_score, os.market_price,
               m.category, os.created_at, m.end_date,
               os.whale_contribution_raw, os.whale_tx_count, os.sm_registry_raw,
               os.prediction_correct
        FROM oracle_snapshots os
        JOIN markets m ON m.id = os.market_id
        WHERE os.prediction_correct IS NOT NULL
          AND os.created_at >= NOW() - $1 * INTERVAL '1 day'
        ORDER BY os.created_at
        """,
        days,
    )

    snapshots = [dict(r) for r in rows]

    # Dedup по market_id (первый snapshot)
    seen: set = set()
    unique = []
    for s in snapshots:
        mid = s.get("market_id")
        if mid and mid not in seen:
            seen.add(mid)
            unique.append(s)

    print(f"Period: {days} days")
    print(f"Total snapshots: {len(snapshots):,}")
    print(f"Unique markets (resolved): {len(unique):,}")

    # Глобальный dedup: маркет ставится только 1 раз по ПЕРВОЙ подошедшей стратегии
    global_bet_markets: set = set()

    for strat_key, strat_def in STRATEGIES.items():
        strat_filter = strat_def["filter"]
        bankroll = INITIAL_BANKROLL
        peak = bankroll
        max_dd = 0.0
        wins = 0
        losses = 0
        total_pnl = 0.0
        trades = 0
        skipped_floor = 0
        skipped_limit = 0
        skipped_low_stake = 0
        skipped_dup = 0

        daily_spent: dict[str, float] = defaultdict(float)
        daily_log: dict[str, dict] = defaultdict(lambda: {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0})

        for snap in unique:
            market_id = snap.get("market_id")

            # Dedup: не ставить на маркет если другая стратегия уже поставила
            if market_id in global_bet_markets:
                skipped_dup += 1
                continue

            cluster = classify_snapshot(snap)

            if not strat_filter(snap, cluster):
                continue

            oracle = float(snap.get("oracle_score") or 0.5)
            market = float(snap.get("market_price") or 0.5)
            entry_price = market if oracle >= market else (1.0 - market)

            if entry_price < ENTRY_PRICE_FLOOR:
                skipped_floor += 1
                continue

            correct = snap.get("prediction_correct", False)

            stake = calculate_stake(
                tier=cluster.tier,
                expected_accuracy=cluster.expected_accuracy,
                bankroll=bankroll,
                entry_price=entry_price,
            )

            if stake <= 0:
                skipped_low_stake += 1
                continue

            # Daily limit
            day_key = str(snap.get("created_at", ""))[:10]
            if daily_spent[day_key] + stake > DAILY_LIMIT:
                skipped_limit += 1
                continue

            stake = min(stake, bankroll)
            trades += 1
            daily_spent[day_key] += stake
            global_bet_markets.add(market_id)

            if correct:
                pnl = stake * (1.0 / max(entry_price, 0.01) - 1.0)
                wins += 1
                daily_log[day_key]["wins"] += 1
            else:
                pnl = -stake
                losses += 1
                daily_log[day_key]["losses"] += 1

            bankroll += pnl
            total_pnl += pnl
            daily_log[day_key]["trades"] += 1
            daily_log[day_key]["pnl"] += pnl

            peak = max(peak, bankroll)
            dd = (peak - bankroll) / peak if peak > 0 else 0
            max_dd = max(max_dd, dd)

            if bankroll <= 0:
                print(f"  [{strat_key}] BANKRUPT at trade {trades}")
                break

        wr = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0
        roi = (bankroll - INITIAL_BANKROLL) / INITIAL_BANKROLL * 100

        print(f"\n{'='*60}")
        print(f"  {strat_key.upper()} — {strat_def['desc']}")
        print(f"{'='*60}")
        print(f"  Trades:         {trades}")
        print(f"  Wins/Losses:    {wins}W / {losses}L")
        print(f"  Win Rate:       {wr:.1f}%")
        print(f"  Final Bank:     ${bankroll:,.2f}")
        print(f"  Total PnL:      ${total_pnl:,.2f}")
        print(f"  ROI:            {roi:+.1f}%")
        print(f"  Max Drawdown:   {max_dd*100:.1f}%")
        print(f"  Skipped (floor): {skipped_floor}")
        print(f"  Skipped (limit): {skipped_limit}")
        print(f"  Skipped (dup):   {skipped_dup}")
        print(f"  Skipped (stake): {skipped_low_stake}")

        if daily_log:
            print(f"\n  {'Дата':<12} {'Trades':>7} {'W':>4} {'L':>4} {'PnL':>10} {'Spent':>8}")
            print(f"  {'-'*48}")
            for d in sorted(daily_log.keys()):
                dl = daily_log[d]
                print(
                    f"  {d:<12} {dl['trades']:>7} {dl['wins']:>4} {dl['losses']:>4}"
                    f" ${dl['pnl']:>+9,.2f} ${daily_spent[d]:>7,.0f}"
                )

    print(f"\n{'='*60}")
    print(f"  TOTAL: {len(global_bet_markets)} unique markets bet across all strategies")
    print(f"{'='*60}")

    await close_db()


if __name__ == "__main__":
    asyncio.run(main())
