#!/usr/bin/env python3
"""
Maker Flow Ablation — Phase 3 of maker-flow research pipeline.

4 конфига против resolved oracle_snapshots:
  0. Baseline     — без maker flow (текущая система)
  1. maker_ratio  — реальный maker_direction_ratio (не CLOB proxy)
  2. maker_tier   — взвешивание по wallet tier из Phase 2
  3. combo        — maker_ratio × maker_tier (мультипликативный)

Метрики: Sharpe, ROI, Win Rate, DSR, n_trades
Разбивка по категории (Sports / Politics / Crypto / World).

Decision gate: Sharpe lift >15% в ≥2 категориях → GO

Запуск на VPS:
    python3 scripts/maker_ablation.py \
        --flow   /mnt/data/maker_flow/maker_flow.parquet \
        --scores /mnt/data/maker_flow/maker_wallet_scores.parquet \
        --out    /mnt/data/maker_flow/ablation_results.json

Требования: duckdb
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import time
from pathlib import Path

import duckdb

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("maker_ablation")

# ── Paths (defaults) ──────────────────────────────────────────────────────────

FLOW_PARQUET   = Path("/mnt/data/maker_flow/maker_flow.parquet")
SCORES_PARQUET = Path("/mnt/data/maker_flow/maker_wallet_scores.parquet")
SNAPSHOTS_DB   = "postgresql://oraclepoly:oraclepoly@localhost/oraclepoly"  # overridden via --snapshots
OUTPUT_JSON    = Path("/mnt/data/maker_flow/ablation_results.json")

# ── Strategy simulation parameters ───────────────────────────────────────────

# Берём те же пороги что в текущих стратегиях (NO_bias logic)
BASELINE_ENTRY_MIN = 0.85   # min price for NO entries
BASELINE_ENTRY_MAX = 0.97
MIN_USDC_VOLUME    = 5_000  # min market USDC volume (raw /1e6)

# Maker ratio threshold: если реальные makersы BUY > takers BUY → bearish signal
MAKER_BUY_RATIO_THRESHOLD = 0.55  # makersы чаще BUY → рынок переоценён → NO trade

# Tier weights для maker_tier конфига
TIER_WEIGHTS = {
    "whale":   1.5,
    "smart":   1.2,
    "mid":     1.0,
    "retail":  0.8,
}

# DSR (Deflated Sharpe Ratio) correction
# Число пробных стратегий в grid search
N_TRIALS_DSR = 4  # 4 конфига

# Lookback для rolling Sharpe
ROLLING_WINDOW_DAYS = 30


# ── Metrics ───────────────────────────────────────────────────────────────────

def sharpe(returns: list[float]) -> float:
    """Annualised Sharpe (daily returns assumed)."""
    if len(returns) < 10:
        return float("nan")
    n = len(returns)
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    std = math.sqrt(var)
    if std == 0:
        return float("nan")
    return mean / std * math.sqrt(365)


def dsr(sharpe_val: float, n_obs: int, n_trials: int) -> float:
    """
    Deflated Sharpe Ratio (Bailey & Lopez de Prado, 2014).
    Corrects for multiple-testing bias.
    dsr = S* where S* = S / sqrt(1 + (gamma(1/2) * ln(n_trials) / n_obs))
    Simplified version.
    """
    if math.isnan(sharpe_val) or n_obs < 10:
        return float("nan")
    # Expected maximum Sharpe under H0 (iid normal)
    # E[max Sharpe] ≈ sqrt(2*ln(n_trials))  (Gumbel approximation)
    e_max = math.sqrt(2 * math.log(max(n_trials, 2)))
    # Variance correction
    correction = math.sqrt(1 + e_max**2 / (2 * n_obs))
    return sharpe_val / correction


def win_rate(pnls: list[float]) -> float:
    if not pnls:
        return float("nan")
    wins = sum(1 for p in pnls if p > 0)
    return wins / len(pnls)


def roi(pnls: list[float], stakes: list[float]) -> float:
    total_stake = sum(stakes)
    if total_stake == 0:
        return float("nan")
    return sum(pnls) / total_stake


# ── Core ablation logic ───────────────────────────────────────────────────────

def build_signal_tables(
    db: duckdb.DuckDBPyConnection,
    flow_path: Path,
    scores_path: Path,
    snapshots_parquet: Path,
) -> None:
    """
    Строим вспомогательные таблицы из maker_flow и snapshots.
    snapshots_parquet: экспорт oracle_snapshots из PostgreSQL.
    """
    log.info("Loading oracle_snapshots...")
    db.execute(f"""
        CREATE OR REPLACE TABLE snapshots AS
        SELECT
            condition_id,
            market_id,
            oracle_score,
            created_at::BIGINT AS created_ts,
            outcome,
            category
        FROM read_parquet('{snapshots_parquet}')
        WHERE outcome IS NOT NULL
          AND oracle_score IS NOT NULL
    """)
    n_snap = db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    log.info(f"Snapshots loaded: {n_snap:,}")

    log.info("Computing maker direction ratios per market per day...")
    db.execute(f"""
        CREATE OR REPLACE TABLE maker_ratios AS
        SELECT
            market_id,
            condition_id,
            DATE_TRUNC('day', TO_TIMESTAMP(timestamp)) AS trade_day,
            -- доля сделок где maker = BUY (он ставит на YES)
            AVG(CASE WHEN maker_direction = 'BUY' THEN 1.0 ELSE 0.0 END)  AS maker_buy_ratio,
            AVG(CASE WHEN taker_direction = 'BUY' THEN 1.0 ELSE 0.0 END)  AS taker_buy_ratio,
            SUM(usdc_amount) / 1e6                                          AS daily_usdc_volume,
            COUNT(*)                                                         AS trade_count
        FROM read_parquet('{flow_path}')
        WHERE is_platform = false
          AND market_id != ''
          AND condition_id != ''
        GROUP BY market_id, condition_id, trade_day
    """)
    n_ratios = db.execute("SELECT COUNT(*) FROM maker_ratios").fetchone()[0]
    log.info(f"Maker ratios: {n_ratios:,} (market×day)")

    if scores_path.exists():
        log.info("Loading maker wallet scores...")
        db.execute(f"""
            CREATE OR REPLACE TABLE wallet_scores AS
            SELECT maker, tier, volume_score, diversity_score
            FROM read_parquet('{scores_path}')
        """)

        log.info("Computing tier-weighted maker ratio per market per day...")
        db.execute(f"""
            CREATE OR REPLACE TABLE maker_tier_ratios AS
            SELECT
                f.market_id,
                f.condition_id,
                DATE_TRUNC('day', TO_TIMESTAMP(f.timestamp)) AS trade_day,
                -- взвешенная BUY ratio (вес = tier_weight × volume_score)
                SUM(
                    CASE WHEN f.maker_direction = 'BUY'
                         THEN COALESCE(w.volume_score, 0.3) *
                              CASE w.tier
                                  WHEN 'whale'  THEN {TIER_WEIGHTS['whale']}
                                  WHEN 'smart'  THEN {TIER_WEIGHTS['smart']}
                                  WHEN 'retail' THEN {TIER_WEIGHTS['retail']}
                                  ELSE               {TIER_WEIGHTS['mid']}
                              END
                         ELSE 0.0
                    END
                ) / NULLIF(COUNT(*), 0)                   AS weighted_maker_buy_ratio,
                COUNT(*)                                   AS trade_count
            FROM read_parquet('{flow_path}') f
            LEFT JOIN wallet_scores w ON f.maker = w.maker
            WHERE f.is_platform = false
              AND f.market_id != ''
              AND f.condition_id != ''
            GROUP BY f.market_id, f.condition_id, trade_day
        """)
    else:
        log.warning(f"Scores parquet not found ({scores_path}), skipping tier config")


def run_config(
    db: duckdb.DuckDBPyConnection,
    config: int,
    categories: list[str],
) -> dict:
    """
    Запустить один конфиг ablation. config: 0=baseline, 1=ratio, 2=tier, 3=combo.
    Returns results dict.
    """
    config_names = {
        0: "baseline",
        1: "maker_ratio",
        2: "maker_tier",
        3: "combo",
    }
    name = config_names[config]
    log.info(f"Running config: {name}")

    # Базовый JOIN: snapshots × maker signals
    # Для каждого snapshot берём сигналы за 3 дня до
    if config == 0:
        # Baseline: только oracle_score
        db.execute("""
            CREATE OR REPLACE TABLE entries AS
            SELECT
                s.condition_id,
                s.market_id,
                s.oracle_score,
                s.outcome,
                s.category,
                s.created_ts,
                NULL::DOUBLE AS maker_buy_ratio,
                NULL::DOUBLE AS weighted_buy_ratio,
                1.0          AS signal_multiplier
            FROM snapshots s
        """)
    elif config == 1:
        db.execute("""
            CREATE OR REPLACE TABLE entries AS
            SELECT
                s.condition_id,
                s.market_id,
                s.oracle_score,
                s.outcome,
                s.category,
                s.created_ts,
                AVG(r.maker_buy_ratio)  AS maker_buy_ratio,
                NULL::DOUBLE            AS weighted_buy_ratio,
                -- если makersы чаще BUY → NO сигнал сильнее → multiplier > 1
                CASE
                    WHEN AVG(r.maker_buy_ratio) > 0.55 THEN 1.2
                    WHEN AVG(r.maker_buy_ratio) < 0.45 THEN 0.8
                    ELSE 1.0
                END AS signal_multiplier
            FROM snapshots s
            LEFT JOIN maker_ratios r
                ON r.condition_id = s.condition_id
               AND r.trade_day >= TO_TIMESTAMP(s.created_ts - 3*86400)
               AND r.trade_day <= TO_TIMESTAMP(s.created_ts)
            GROUP BY s.condition_id, s.market_id, s.oracle_score,
                     s.outcome, s.category, s.created_ts
        """)
    elif config == 2:
        db.execute("""
            CREATE OR REPLACE TABLE entries AS
            SELECT
                s.condition_id,
                s.market_id,
                s.oracle_score,
                s.outcome,
                s.category,
                s.created_ts,
                NULL::DOUBLE                      AS maker_buy_ratio,
                AVG(t.weighted_maker_buy_ratio)   AS weighted_buy_ratio,
                CASE
                    WHEN AVG(t.weighted_maker_buy_ratio) > 0.55 THEN 1.2
                    WHEN AVG(t.weighted_maker_buy_ratio) < 0.45 THEN 0.8
                    ELSE 1.0
                END AS signal_multiplier
            FROM snapshots s
            LEFT JOIN maker_tier_ratios t
                ON t.condition_id = s.condition_id
               AND t.trade_day >= TO_TIMESTAMP(s.created_ts - 3*86400)
               AND t.trade_day <= TO_TIMESTAMP(s.created_ts)
            GROUP BY s.condition_id, s.market_id, s.oracle_score,
                     s.outcome, s.category, s.created_ts
        """)
    elif config == 3:
        db.execute("""
            CREATE OR REPLACE TABLE entries AS
            SELECT
                s.condition_id,
                s.market_id,
                s.oracle_score,
                s.outcome,
                s.category,
                s.created_ts,
                AVG(r.maker_buy_ratio)           AS maker_buy_ratio,
                AVG(t.weighted_maker_buy_ratio)  AS weighted_buy_ratio,
                -- combo: оба сигнала должны согласоваться
                CASE
                    WHEN AVG(r.maker_buy_ratio) > 0.55
                     AND AVG(t.weighted_maker_buy_ratio) > 0.55 THEN 1.35
                    WHEN AVG(r.maker_buy_ratio) < 0.45
                     AND AVG(t.weighted_maker_buy_ratio) < 0.45 THEN 0.70
                    WHEN AVG(r.maker_buy_ratio) > 0.55
                      OR AVG(t.weighted_maker_buy_ratio) > 0.55 THEN 1.1
                    ELSE 1.0
                END AS signal_multiplier
            FROM snapshots s
            LEFT JOIN maker_ratios r
                ON r.condition_id = s.condition_id
               AND r.trade_day >= TO_TIMESTAMP(s.created_ts - 3*86400)
               AND r.trade_day <= TO_TIMESTAMP(s.created_ts)
            LEFT JOIN maker_tier_ratios t
                ON t.condition_id = s.condition_id
               AND t.trade_day >= TO_TIMESTAMP(s.created_ts - 3*86400)
               AND t.trade_day <= TO_TIMESTAMP(s.created_ts)
            GROUP BY s.condition_id, s.market_id, s.oracle_score,
                     s.outcome, s.category, s.created_ts
        """)

    # Симуляция: NO entry если oracle_score > threshold
    # P&L: если outcome=NO → win (1 - price), если outcome=YES → lose (-price)
    results = {}

    for cat in (categories + ["ALL"]):
        cat_filter = "" if cat == "ALL" else f"AND category = '{cat}'"

        trades = db.execute(f"""
            SELECT
                oracle_score,
                outcome,
                signal_multiplier,
                created_ts
            FROM entries
            WHERE oracle_score BETWEEN {BASELINE_ENTRY_MIN} AND {BASELINE_ENTRY_MAX}
            {cat_filter}
            ORDER BY created_ts
        """).fetchall()

        if len(trades) < 10:
            results[cat] = {"n": len(trades), "note": "insufficient data"}
            continue

        pnls = []
        stakes = []
        daily_pnl: dict[int, float] = {}

        for oracle_score, outcome, signal_mult, ts in trades:
            if oracle_score is None or outcome is None:
                continue

            # Adjusted signal: oracle_score × multiplier (cap to valid range)
            adj_score = min(0.99, oracle_score * (signal_mult or 1.0))

            # Entry: BUY NO at price = adj_score → stake = 1 unit
            stake = 1.0
            if outcome == "NO":
                pnl = (1.0 - adj_score) / adj_score * stake   # win: pays (1-p)/p
            elif outcome == "YES":
                pnl = -stake                                    # lose stake
            else:
                continue

            pnls.append(pnl)
            stakes.append(stake)

            day = int(ts) // 86400
            daily_pnl[day] = daily_pnl.get(day, 0.0) + pnl

        daily_returns = list(daily_pnl.values())
        s = sharpe(daily_returns)
        d = dsr(s, len(daily_returns), N_TRIALS_DSR)

        results[cat] = {
            "n":        len(pnls),
            "win_rate": round(win_rate(pnls), 4),
            "roi":      round(roi(pnls, stakes), 4),
            "pnl_sum":  round(sum(pnls), 2),
            "sharpe":   round(s, 3) if not math.isnan(s) else None,
            "dsr":      round(d, 3) if not math.isnan(d) else None,
            "n_days":   len(daily_pnl),
        }
        log.info(
            f"  [{name}] {cat:12s}: n={results[cat]['n']:5d} "
            f"wr={results[cat]['win_rate']:.2%} "
            f"roi={results[cat]['roi']:.2%} "
            f"sharpe={results[cat]['sharpe']} "
            f"dsr={results[cat]['dsr']}"
        )

    return {"config": name, "results": results}


def print_decision_gate(all_results: list[dict]) -> dict:
    """
    Decision gate: Sharpe lift >15% в ≥2 категориях → GO.
    Returns decision dict.
    """
    log.info("=" * 60)
    log.info("DECISION GATE: Sharpe lift >15% in ≥2 categories → GO")
    log.info("=" * 60)

    baseline = next(r for r in all_results if r["config"] == "baseline")
    categories = [k for k in baseline["results"] if k != "ALL"]

    best_config = None
    best_lift_count = 0
    decision_table = {}

    for result in all_results:
        if result["config"] == "baseline":
            continue
        name = result["config"]
        lift_count = 0
        cat_lifts = {}

        for cat in categories:
            b = baseline["results"].get(cat, {})
            r = result["results"].get(cat, {})
            b_sharpe = b.get("sharpe")
            r_sharpe = r.get("sharpe")

            if b_sharpe and r_sharpe and b_sharpe != 0:
                lift = (r_sharpe - b_sharpe) / abs(b_sharpe)
                cat_lifts[cat] = round(lift, 3)
                if lift > 0.15:
                    lift_count += 1
            else:
                cat_lifts[cat] = None

        decision_table[name] = {
            "lifts": cat_lifts,
            "lift_count_ge15pct": lift_count,
            "go": lift_count >= 2,
        }

        if lift_count > best_lift_count:
            best_lift_count = lift_count
            best_config = name

        log.info(f"Config {name}: {lift_count} cats with >15% Sharpe lift → {'GO ✅' if lift_count >= 2 else 'NO GO ❌'}")
        for cat, lift in cat_lifts.items():
            marker = "✅" if (lift is not None and lift > 0.15) else "  "
            log.info(f"  {marker} {cat:12s}: lift={lift}")

    overall_go = any(v["go"] for v in decision_table.values())
    log.info(f"\nOVERALL DECISION: {'GO ✅ — integrate maker flow' if overall_go else 'NO GO ❌ — skip maker flow'}")

    return {
        "overall_go": overall_go,
        "best_config": best_config,
        "configs": decision_table,
    }


def main():
    parser = argparse.ArgumentParser(description="Maker flow ablation — Phase 3")
    parser.add_argument("--flow",      type=Path, default=FLOW_PARQUET)
    parser.add_argument("--scores",    type=Path, default=SCORES_PARQUET)
    parser.add_argument("--snapshots", type=Path, required=True,
                        help="Path to oracle_snapshots.parquet (export from PostgreSQL)")
    parser.add_argument("--out",       type=Path, default=OUTPUT_JSON)
    parser.add_argument("--categories", nargs="+",
                        default=["Sports", "Politics", "Crypto", "World"],
                        help="Market categories to evaluate")
    parser.add_argument("--configs", nargs="+", type=int,
                        default=[0, 1, 2, 3],
                        help="Which configs to run (0=baseline 1=ratio 2=tier 3=combo)")
    args = parser.parse_args()

    for path, label in [(args.flow, "--flow"), (args.snapshots, "--snapshots")]:
        if not path.exists():
            log.error(f"File not found: {path} ({label})")
            raise SystemExit(1)

    log.info("Phase 3: Maker Flow Ablation Matrix")
    log.info(f"Flow:      {args.flow}")
    log.info(f"Scores:    {args.scores}")
    log.info(f"Snapshots: {args.snapshots}")
    log.info(f"Configs:   {args.configs}")
    log.info(f"Categories: {args.categories}")

    db = duckdb.connect(":memory:")
    db.execute("PRAGMA threads=4")
    db.execute("PRAGMA memory_limit='4GB'")

    build_signal_tables(db, args.flow, args.scores, args.snapshots)

    all_results = []
    for cfg in args.configs:
        if cfg in (2, 3) and not args.scores.exists():
            log.warning(f"Skipping config {cfg} (scores not available)")
            continue
        result = run_config(db, cfg, args.categories)
        all_results.append(result)

    decision = print_decision_gate(all_results)

    output = {
        "run_ts": int(time.time()),
        "run_date": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "flow_path": str(args.flow),
        "scores_path": str(args.scores),
        "snapshots_path": str(args.snapshots),
        "decision": decision,
        "configs": all_results,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, indent=2))
    log.info(f"Results written: {args.out}")

    db.close()
    log.info("Phase 3 done.")


if __name__ == "__main__":
    main()
