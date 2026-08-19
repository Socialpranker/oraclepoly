"""HF Bias Verification — верификация ключевых claims из deep research.

Источник данных: market_enriched.parquet + market_aggregates.parquet
(VPS: /mnt/data/polymarket_dataset/)

Гипотезы из deep research (2026-04-13):
  H1: YES-bias — рынки системно переоценивают вероятность YES
  H2: Longshot bias — дешёвые контракты (<10c) resolve реже implied probability
  H3: Category-specific bias — Politics/Sports сильнее Crypto
  H4: Horizon effect — короткие рынки (<7d) имеют больший bias
  H5: Volume effect — высоковолюмные рынки эффективнее
  H6: Always-NO ROI — PnL для always-NO по категориям и buckets
  H7: Maker vs Taker edge — кто зарабатывает (на aggregated PnL)

Запуск:
  ssh root@<YOUR_SERVER_IP>
  cd ~/oracle && source .venv/bin/activate
  python scripts/research/hf_bias_verification.py [--data-dir /mnt/data/polymarket_dataset]
"""

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import duckdb


DEFAULT_DATA_DIR = "/mnt/data/polymarket_dataset"


def main() -> None:
    parser = argparse.ArgumentParser(description="HF Bias Verification")
    parser.add_argument("--data-dir", type=str, default=DEFAULT_DATA_DIR)
    parser.add_argument("--output", type=str, default=None,
                        help="JSON output path (default: <data-dir>/bias_verification_<date>.json)")
    parser.add_argument("--min-volume", type=float, default=1000,
                        help="Min total_volume_usd for market inclusion (default: 1000)")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    enriched = data_dir / "market_enriched.parquet"
    aggregates = data_dir / "market_aggregates.parquet"

    if not enriched.exists():
        print(f"ERROR: {enriched} not found")
        return
    if not aggregates.exists():
        print(f"ERROR: {aggregates} not found")
        return

    output_path = args.output or str(data_dir / f"bias_verification_{datetime.now(timezone.utc):%Y%m%d}.json")

    print("=" * 70)
    print("HF BIAS VERIFICATION — Deep Research Claims")
    print(f"Data: {data_dir}")
    print(f"Min volume: ${args.min_volume:,.0f}")
    print("=" * 70)

    con = duckdb.connect()
    con.execute("SET memory_limit='3GB'")
    con.execute("SET threads=3")

    # ── Загрузка resolved рынков с volume filter ──
    # winner — VARCHAR ("Yes","No","Up","Down","Over","Under", etc.)
    # answer1 — первый ответ (обычно YES/Up/Over). Если winner=answer1 → YES wins.
    rows = con.execute(f"""
        SELECT
            m.condition_id,
            m.category,
            CASE WHEN m.winner = m.answer1 THEN true
                 WHEN m.winner = m.answer2 THEN false
                 ELSE NULL END as winner_is_yes,
            m.end_date,
            m.created_at,
            a.total_volume_usd,
            a.avg_price,
            a.vwap,
            a.trade_count,
            a.whale_trade_count,
            a.taker_buy_volume,
            a.taker_sell_volume,
            a.price_stddev
        FROM read_parquet('{enriched}') m
        JOIN read_parquet('{aggregates}') a
            ON m.condition_id = a.condition_id
        WHERE m.is_resolved = true
          AND m.winner IS NOT NULL
          AND (m.winner = m.answer1 OR m.winner = m.answer2)
          AND a.total_volume_usd >= {args.min_volume}
    """).fetchall()

    cols = [
        "condition_id", "category", "winner_is_yes", "end_date", "created_at",
        "total_volume_usd", "avg_price", "vwap", "trade_count",
        "whale_trade_count", "taker_buy_volume", "taker_sell_volume", "price_stddev"
    ]

    n_total = len(rows)
    print(f"\nLoaded {n_total:,} resolved markets (volume >= ${args.min_volume:,.0f})")

    if n_total == 0:
        print("ERROR: No markets found. Check data.")
        return

    # ── Подготовка данных ──
    markets = []
    for r in rows:
        d = dict(zip(cols, r))
        # Вычисляем horizon (days)
        if d["end_date"] and d["created_at"]:
            try:
                end = d["end_date"] if isinstance(d["end_date"], datetime) else datetime.fromisoformat(str(d["end_date"]))
                start = d["created_at"] if isinstance(d["created_at"], datetime) else datetime.fromisoformat(str(d["created_at"]))
                d["horizon_days"] = max((end - start).total_seconds() / 86400, 0.01)
            except Exception:
                d["horizon_days"] = None
        else:
            d["horizon_days"] = None
        markets.append(d)

    results = {}

    # ═══════════════════════════════════════════════════════════
    # H1: YES-BIAS — сравнение actual YES rate vs implied probability
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("H1: YES-BIAS (actual YES% vs implied probability)")
    print("=" * 70)

    # Используем VWAP как proxy для "implied probability рынка"
    # Bucket по VWAP: 0-10, 10-20, ..., 90-100
    buckets_h1 = {}
    for m in markets:
        vwap = m["vwap"]
        if vwap is None or vwap < 0.001 or vwap > 0.999:
            continue
        bucket = min(int(vwap * 10), 9)  # 0-9
        bucket_label = f"{bucket*10}-{(bucket+1)*10}%"
        if bucket_label not in buckets_h1:
            buckets_h1[bucket_label] = {"n": 0, "yes_wins": 0, "sum_vwap": 0.0}
        buckets_h1[bucket_label]["n"] += 1
        buckets_h1[bucket_label]["yes_wins"] += 1 if m["winner_is_yes"] else 0
        buckets_h1[bucket_label]["sum_vwap"] += vwap

    print(f"\n  {'VWAP Bucket':<12} {'N':>6} {'YES wins':>9} {'Actual%':>8} {'Implied%':>9} {'Bias':>8}")
    print(f"  {'-'*12} {'-'*6} {'-'*9} {'-'*8} {'-'*9} {'-'*8}")

    h1_results = []
    for bucket in sorted(buckets_h1.keys()):
        b = buckets_h1[bucket]
        actual = b["yes_wins"] / b["n"]
        implied = b["sum_vwap"] / b["n"]
        bias = actual - implied
        print(f"  {bucket:<12} {b['n']:>6,} {b['yes_wins']:>9,} {actual:>7.1%} {implied:>8.1%} {bias:>+7.1%}")
        h1_results.append({
            "bucket": bucket, "n": b["n"], "yes_wins": b["yes_wins"],
            "actual_yes_rate": round(actual, 4), "implied_prob": round(implied, 4),
            "bias": round(bias, 4)
        })

    # Global YES bias
    total_yes = sum(1 for m in markets if m["winner_is_yes"])
    global_yes_rate = total_yes / n_total
    avg_vwap = sum(m["vwap"] for m in markets if m["vwap"]) / sum(1 for m in markets if m["vwap"])
    print(f"\n  GLOBAL: YES wins {total_yes:,}/{n_total:,} = {global_yes_rate:.1%}, avg implied (VWAP) = {avg_vwap:.1%}")
    print(f"  GLOBAL BIAS: {global_yes_rate - avg_vwap:+.1%}")

    results["H1_yes_bias"] = {
        "buckets": h1_results,
        "global_yes_rate": round(global_yes_rate, 4),
        "global_avg_implied": round(avg_vwap, 4),
        "global_bias": round(global_yes_rate - avg_vwap, 4),
        "n_markets": n_total
    }

    # ═══════════════════════════════════════════════════════════
    # H2: LONGSHOT BIAS — дешёвые контракты: implied vs actual
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("H2: LONGSHOT BIAS (fine-grained <30c)")
    print("=" * 70)

    longshot_buckets = {
        "0-1%": (0, 0.01), "1-3%": (0.01, 0.03), "3-5%": (0.03, 0.05),
        "5-10%": (0.05, 0.10), "10-15%": (0.10, 0.15), "15-20%": (0.15, 0.20),
        "20-30%": (0.20, 0.30), "70-80%": (0.70, 0.80), "80-85%": (0.80, 0.85),
        "85-90%": (0.85, 0.90), "90-95%": (0.90, 0.95), "95-97%": (0.95, 0.97),
        "97-99%": (0.97, 0.99), "99-100%": (0.99, 1.0),
    }

    h2_results = []
    print(f"\n  {'Bucket':<10} {'N':>6} {'YES wins':>9} {'Actual%':>8} {'Implied%':>9} {'Overpriced?':>12}")
    print(f"  {'-'*10} {'-'*6} {'-'*9} {'-'*8} {'-'*9} {'-'*12}")

    for label, (lo, hi) in sorted(longshot_buckets.items(), key=lambda x: x[1][0]):
        subset = [m for m in markets if m["vwap"] is not None and lo <= m["vwap"] < hi]
        if not subset:
            continue
        n = len(subset)
        yes_wins = sum(1 for m in subset if m["winner_is_yes"])
        actual = yes_wins / n
        implied = sum(m["vwap"] for m in subset) / n
        overpriced = "YES overpriced" if actual < implied else "NO overpriced" if actual > implied else "fair"
        print(f"  {label:<10} {n:>6,} {yes_wins:>9,} {actual:>7.1%} {implied:>8.1%} {overpriced:>12}")
        h2_results.append({
            "bucket": label, "n": n, "yes_wins": yes_wins,
            "actual": round(actual, 4), "implied": round(implied, 4),
            "overpriced": overpriced
        })

    results["H2_longshot_bias"] = h2_results

    # ═══════════════════════════════════════════════════════════
    # H3: CATEGORY BIAS — YES-bias по категориям
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("H3: CATEGORY-SPECIFIC YES-BIAS")
    print("=" * 70)

    cat_data = {}
    for m in markets:
        cat = m["category"] or "Unknown"
        if cat not in cat_data:
            cat_data[cat] = {"n": 0, "yes_wins": 0, "sum_vwap": 0.0}
        cat_data[cat]["n"] += 1
        cat_data[cat]["yes_wins"] += 1 if m["winner_is_yes"] else 0
        if m["vwap"]:
            cat_data[cat]["sum_vwap"] += m["vwap"]

    print(f"\n  {'Category':<20} {'N':>6} {'YES%':>7} {'Implied%':>9} {'Bias':>8} {'NO-edge':>9}")
    print(f"  {'-'*20} {'-'*6} {'-'*7} {'-'*9} {'-'*8} {'-'*9}")

    h3_results = []
    for cat in sorted(cat_data.keys(), key=lambda c: -cat_data[c]["n"]):
        d = cat_data[cat]
        if d["n"] < 10:
            continue
        actual = d["yes_wins"] / d["n"]
        implied = d["sum_vwap"] / d["n"] if d["sum_vwap"] > 0 else 0
        bias = actual - implied
        # NO-edge: если ставишь всегда NO, сколько ты выигрываешь?
        no_wins = d["n"] - d["yes_wins"]
        no_edge = no_wins / d["n"]  # WR of always-NO
        print(f"  {cat:<20} {d['n']:>6,} {actual:>6.1%} {implied:>8.1%} {bias:>+7.1%} {no_edge:>8.1%}")
        h3_results.append({
            "category": cat, "n": d["n"], "yes_rate": round(actual, 4),
            "implied": round(implied, 4), "bias": round(bias, 4),
            "always_no_wr": round(no_edge, 4)
        })

    results["H3_category_bias"] = h3_results

    # ═══════════════════════════════════════════════════════════
    # H4: HORIZON EFFECT — bias по time-to-resolution
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("H4: HORIZON EFFECT (bias vs time-to-resolution)")
    print("=" * 70)

    horizon_buckets = {
        "<1d": (0, 1), "1-3d": (1, 3), "3-7d": (3, 7),
        "7-14d": (7, 14), "14-30d": (14, 30), "30-90d": (30, 90),
        "90-365d": (90, 365), ">365d": (365, 99999),
    }

    h4_results = []
    print(f"\n  {'Horizon':<10} {'N':>6} {'YES%':>7} {'Implied%':>9} {'Bias':>8} {'NO WR':>7}")
    print(f"  {'-'*10} {'-'*6} {'-'*7} {'-'*9} {'-'*8} {'-'*7}")

    for label, (lo, hi) in sorted(horizon_buckets.items(), key=lambda x: x[1][0]):
        subset = [m for m in markets if m["horizon_days"] is not None and lo <= m["horizon_days"] < hi]
        if not subset:
            continue
        n = len(subset)
        yes_wins = sum(1 for m in subset if m["winner_is_yes"])
        actual = yes_wins / n
        vwap_sum = sum(m["vwap"] for m in subset if m["vwap"])
        vwap_n = sum(1 for m in subset if m["vwap"])
        implied = vwap_sum / vwap_n if vwap_n > 0 else 0
        bias = actual - implied
        no_wr = (n - yes_wins) / n
        print(f"  {label:<10} {n:>6,} {actual:>6.1%} {implied:>8.1%} {bias:>+7.1%} {no_wr:>6.1%}")
        h4_results.append({
            "horizon": label, "n": n, "yes_rate": round(actual, 4),
            "implied": round(implied, 4), "bias": round(bias, 4),
            "always_no_wr": round(no_wr, 4)
        })

    results["H4_horizon_effect"] = h4_results

    # ═══════════════════════════════════════════════════════════
    # H5: VOLUME EFFECT — высоковолюмные рынки эффективнее?
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("H5: VOLUME EFFECT (efficiency vs volume)")
    print("=" * 70)

    volume_buckets = {
        "$1K-5K": (1000, 5000), "$5K-25K": (5000, 25000),
        "$25K-100K": (25000, 100000), "$100K-500K": (100000, 500000),
        "$500K-1M": (500000, 1_000_000), ">$1M": (1_000_000, 1e18),
    }

    h5_results = []
    print(f"\n  {'Volume':<12} {'N':>6} {'YES%':>7} {'Implied%':>9} {'|Bias|':>8} {'Brier':>7}")
    print(f"  {'-'*12} {'-'*6} {'-'*7} {'-'*9} {'-'*8} {'-'*7}")

    for label, (lo, hi) in sorted(volume_buckets.items(), key=lambda x: x[1][0]):
        subset = [m for m in markets if m["total_volume_usd"] is not None and lo <= m["total_volume_usd"] < hi]
        if not subset:
            continue
        n = len(subset)
        yes_wins = sum(1 for m in subset if m["winner_is_yes"])
        actual = yes_wins / n
        vwap_vals = [m["vwap"] for m in subset if m["vwap"] is not None]
        implied = sum(vwap_vals) / len(vwap_vals) if vwap_vals else 0
        bias = actual - implied
        # Brier score (VWAP vs outcome)
        brier_sum = 0
        brier_n = 0
        for m in subset:
            if m["vwap"] is not None:
                outcome = 1.0 if m["winner_is_yes"] else 0.0
                brier_sum += (m["vwap"] - outcome) ** 2
                brier_n += 1
        brier = brier_sum / brier_n if brier_n > 0 else 0
        print(f"  {label:<12} {n:>6,} {actual:>6.1%} {implied:>8.1%} {abs(bias):>7.1%} {brier:>6.3f}")
        h5_results.append({
            "volume_bucket": label, "n": n, "yes_rate": round(actual, 4),
            "implied": round(implied, 4), "abs_bias": round(abs(bias), 4),
            "brier_score": round(brier, 4)
        })

    results["H5_volume_effect"] = h5_results

    # ═══════════════════════════════════════════════════════════
    # H6: ALWAYS-NO ROI — PnL для always-NO по сегментам
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("H6: ALWAYS-NO ROI (по категориям x горизонтам)")
    print("=" * 70)

    # PnL для always-NO: entry = (1 - VWAP), payout = $1 if NO wins
    # ROI = (1 / entry) - 1 if win, -1 if lose
    def no_pnl(vwap: float, winner_is_yes: bool) -> tuple[float, bool]:
        """PnL per $1 для always-NO."""
        entry = max(1.0 - vwap, 0.01)
        if not winner_is_yes:  # NO wins
            return (1.0 / entry) - 1.0, True
        return -1.0, False

    # По категориям
    cat_no_pnl = {}
    for m in markets:
        cat = m["category"] or "Unknown"
        if m["vwap"] is None:
            continue
        pnl, win = no_pnl(m["vwap"], m["winner_is_yes"])
        if cat not in cat_no_pnl:
            cat_no_pnl[cat] = {"pnls": [], "wins": []}
        cat_no_pnl[cat]["pnls"].append(pnl)
        cat_no_pnl[cat]["wins"].append(win)

    print(f"\n  {'Category':<20} {'N':>6} {'WR':>7} {'ROI':>9} {'Sharpe':>8} {'Total PnL':>10}")
    print(f"  {'-'*20} {'-'*6} {'-'*7} {'-'*9} {'-'*8} {'-'*10}")

    h6_results = []
    for cat in sorted(cat_no_pnl.keys(), key=lambda c: -len(cat_no_pnl[c]["pnls"])):
        d = cat_no_pnl[cat]
        n = len(d["pnls"])
        if n < 10:
            continue
        wr = sum(d["wins"]) / n
        roi = sum(d["pnls"]) / n
        total_pnl = sum(d["pnls"])
        mean = roi
        var = sum((p - mean) ** 2 for p in d["pnls"]) / max(n - 1, 1)
        sharpe = roi / math.sqrt(var) if var > 0 else 0
        print(f"  {cat:<20} {n:>6,} {wr:>6.1%} {roi:>+8.4f} {sharpe:>+7.3f} {total_pnl:>+9.1f}")
        h6_results.append({
            "category": cat, "n": n, "wr": round(wr, 4),
            "roi": round(roi, 4), "sharpe": round(sharpe, 4),
            "total_pnl": round(total_pnl, 2)
        })

    # По горизонтам
    print(f"\n  {'Horizon':<10} {'N':>6} {'WR':>7} {'ROI':>9} {'Sharpe':>8}")
    print(f"  {'-'*10} {'-'*6} {'-'*7} {'-'*9} {'-'*8}")

    for label, (lo, hi) in sorted(horizon_buckets.items(), key=lambda x: x[1][0]):
        subset = [m for m in markets if m["horizon_days"] is not None and lo <= m["horizon_days"] < hi and m["vwap"] is not None]
        if len(subset) < 10:
            continue
        pnls = []
        wins = []
        for m in subset:
            p, w = no_pnl(m["vwap"], m["winner_is_yes"])
            pnls.append(p)
            wins.append(w)
        n = len(pnls)
        wr = sum(wins) / n
        roi = sum(pnls) / n
        mean = roi
        var = sum((p - mean) ** 2 for p in pnls) / max(n - 1, 1)
        sharpe = roi / math.sqrt(var) if var > 0 else 0
        print(f"  {label:<10} {n:>6,} {wr:>6.1%} {roi:>+8.4f} {sharpe:>+7.3f}")

    results["H6_always_no_roi"] = h6_results

    # ═══════════════════════════════════════════════════════════
    # H7: MAKER vs TAKER — кто зарабатывает?
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("H7: MAKER vs TAKER EDGE")
    print("=" * 70)

    maker_pnl_path = data_dir / "maker_pnl.parquet"
    taker_pnl_path = data_dir / "taker_pnl.parquet"

    h7_results = {}

    if maker_pnl_path.exists() and taker_pnl_path.exists():
        # Aggregate maker PnL
        maker_stats = con.execute(f"""
            SELECT
                COUNT(*) as n_wallets,
                SUM(realized_pnl) as total_pnl,
                AVG(realized_pnl) as avg_pnl,
                PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY realized_pnl) as median_pnl,
                COUNT(*) FILTER (WHERE realized_pnl > 0) as profitable_wallets
            FROM read_parquet('{maker_pnl_path}')
            WHERE volume_usd > 1000
        """).fetchone()

        taker_stats = con.execute(f"""
            SELECT
                COUNT(*) as n_wallets,
                SUM(realized_pnl) as total_pnl,
                AVG(realized_pnl) as avg_pnl,
                PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY realized_pnl) as median_pnl,
                COUNT(*) FILTER (WHERE realized_pnl > 0) as profitable_wallets
            FROM read_parquet('{taker_pnl_path}')
            WHERE volume_usd > 1000
        """).fetchone()

        print(f"\n  {'Role':<8} {'Wallets':>8} {'Profitable%':>12} {'Total PnL':>12} {'Avg PnL':>10} {'Median PnL':>11}")
        print(f"  {'-'*8} {'-'*8} {'-'*12} {'-'*12} {'-'*10} {'-'*11}")

        if maker_stats and maker_stats[0] > 0:
            pct_profit_m = maker_stats[4] / maker_stats[0]
            print(f"  {'Maker':<8} {maker_stats[0]:>8,} {pct_profit_m:>11.1%} {maker_stats[1]:>+11,.0f} {maker_stats[2]:>+9,.0f} {maker_stats[3]:>+10,.0f}")
            h7_results["maker"] = {
                "n_wallets": maker_stats[0], "total_pnl": round(float(maker_stats[1]), 2),
                "avg_pnl": round(float(maker_stats[2]), 2), "median_pnl": round(float(maker_stats[3]), 2),
                "profitable_pct": round(pct_profit_m, 4)
            }

        if taker_stats and taker_stats[0] > 0:
            pct_profit_t = taker_stats[4] / taker_stats[0]
            print(f"  {'Taker':<8} {taker_stats[0]:>8,} {pct_profit_t:>11.1%} {taker_stats[1]:>+11,.0f} {taker_stats[2]:>+9,.0f} {taker_stats[3]:>+10,.0f}")
            h7_results["taker"] = {
                "n_wallets": taker_stats[0], "total_pnl": round(float(taker_stats[1]), 2),
                "avg_pnl": round(float(taker_stats[2]), 2), "median_pnl": round(float(taker_stats[3]), 2),
                "profitable_pct": round(pct_profit_t, 4)
            }
    else:
        print("  ⚠️  maker_pnl.parquet / taker_pnl.parquet not found — skipping H7")

    results["H7_maker_taker"] = h7_results

    # ═══════════════════════════════════════════════════════════
    # H8: CROSS-TAB — Category x Horizon x VWAP bucket → NO ROI
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("H8: BEST SEGMENTS (Category x Horizon x Price → NO ROI)")
    print("=" * 70)

    # Только топ категории
    top_cats = ["Sports", "Crypto", "Politics", "Pop Culture", "Science & Tech"]
    horizon_labels = {"short": (0, 7), "mid": (7, 30), "long": (30, 99999)}
    price_labels = {"cheap_NO": (0, 0.15), "mid_NO": (0.15, 0.50), "expensive_NO": (0.50, 0.85), "favorite_NO": (0.85, 1.0)}

    segments = []
    for cat in top_cats:
        for hlabel, (hlo, hhi) in horizon_labels.items():
            for plabel, (plo, phi) in price_labels.items():
                subset = [
                    m for m in markets
                    if m["category"] == cat
                    and m["horizon_days"] is not None and hlo <= m["horizon_days"] < hhi
                    and m["vwap"] is not None and plo <= m["vwap"] < phi
                ]
                if len(subset) < 20:
                    continue
                pnls = []
                wins = []
                for m in subset:
                    p, w = no_pnl(m["vwap"], m["winner_is_yes"])
                    pnls.append(p)
                    wins.append(w)
                n = len(pnls)
                wr = sum(wins) / n
                roi = sum(pnls) / n
                segments.append({
                    "category": cat, "horizon": hlabel, "price_bucket": plabel,
                    "n": n, "wr": round(wr, 4), "roi": round(roi, 4)
                })

    # Топ-15 по ROI
    segments.sort(key=lambda x: -x["roi"])
    print(f"\n  TOP-15 segments for always-NO:")
    print(f"  {'Category':<16} {'Horizon':<8} {'Price':<14} {'N':>5} {'WR':>6} {'ROI':>9}")
    print(f"  {'-'*16} {'-'*8} {'-'*14} {'-'*5} {'-'*6} {'-'*9}")
    for seg in segments[:15]:
        print(f"  {seg['category']:<16} {seg['horizon']:<8} {seg['price_bucket']:<14} {seg['n']:>5,} {seg['wr']:>5.1%} {seg['roi']:>+8.4f}")

    # Worst-5 (для сравнения)
    print(f"\n  WORST-5 segments for always-NO:")
    for seg in segments[-5:]:
        print(f"  {seg['category']:<16} {seg['horizon']:<8} {seg['price_bucket']:<14} {seg['n']:>5,} {seg['wr']:>5.1%} {seg['roi']:>+8.4f}")

    results["H8_best_segments"] = segments

    # ═══════════════════════════════════════════════════════════
    # SUMMARY
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    global_bias = results["H1_yes_bias"]["global_bias"]
    print(f"\n  Global YES-bias: {global_bias:+.1%}")
    if global_bias < -0.02:
        print("  → CONFIRMED: markets systematically overprice YES")
        print("  → always-NO has structural edge")
    elif global_bias > 0.02:
        print("  → REJECTED: markets actually underprice YES (NO-bias)")
    else:
        print("  → INCONCLUSIVE: bias within ±2%")

    # Найти категорию с максимальным NO-edge
    if h3_results:
        best_no_cat = max(h3_results, key=lambda x: x["always_no_wr"])
        worst_no_cat = min(h3_results, key=lambda x: x["always_no_wr"])
        print(f"\n  Best category for NO:  {best_no_cat['category']} (NO WR = {best_no_cat['always_no_wr']:.1%})")
        print(f"  Worst category for NO: {worst_no_cat['category']} (NO WR = {worst_no_cat['always_no_wr']:.1%})")

    if segments:
        best_seg = segments[0]
        print(f"\n  Best NO segment: {best_seg['category']} / {best_seg['horizon']} / {best_seg['price_bucket']}")
        print(f"    N={best_seg['n']}, WR={best_seg['wr']:.1%}, ROI={best_seg['roi']:+.4f}")

    # ── Save JSON ──
    results["_meta"] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "n_markets": n_total,
        "min_volume": args.min_volume,
        "data_dir": str(data_dir),
    }

    with open(output_path, "w") as f:
        json.dump(results, f, indent=2, default=str)

    print(f"\n  Results saved to: {output_path}")
    print("=" * 70)


if __name__ == "__main__":
    main()
