#!/usr/bin/env python3
"""E1: BTC CEX Mirror prediction.

Для resolved Polymarket markets "Will BTC be above $X on DATE" or "BTC reach $X":
1. Parse threshold $X и target date.
2. Fetch BTC daily close около T-24h до resolution.
3. Compute GBM probability using 30d realized vol.
4. Compare с Polymarket implied price.

Edge: |true_p - market_p| > 5pp → bet.
"""
from __future__ import annotations
import asyncio, os, re, time, sys, math, asyncpg, httpx, json
from datetime import datetime, timezone, timedelta

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

BTC_KLINES_URL = "https://api.binance.com/api/v3/klines"

# Regex: "above $X" / "below $X" / "hit $X" / "reach $X"
THRESHOLD_RE = re.compile(r'\$?([\d,]+(?:\.\d+)?)[Kk]?')
ABOVE_PATTERNS = ['above', 'over', 'higher than', 'greater than', 'reach', 'hit']
BELOW_PATTERNS = ['below', 'under', 'less than', 'lower than', 'dip to']


def parse_btc_market(question: str) -> dict | None:
    """Возвращает {threshold: float, direction: 'above'|'below'} или None."""
    q = question.lower()
    if not ('bitcoin' in q or 'btc' in q):
        return None
    direction = None
    for p in ABOVE_PATTERNS:
        if p in q:
            direction = 'above'
            break
    if direction is None:
        for p in BELOW_PATTERNS:
            if p in q:
                direction = 'below'
                break
    if direction is None:
        return None
    # Extract dollar amount
    matches = re.findall(r'\$?([\d,]+)(?:[Kk]|,000)?', question)
    threshold = None
    for m in matches:
        try:
            val = float(m.replace(',', ''))
            if val < 1000:
                val *= 1000  # "150k" → 150000
            if 10000 <= val <= 1000000:  # plausible BTC threshold
                threshold = val
                break
        except ValueError:
            pass
    if threshold is None:
        return None
    return {'threshold': threshold, 'direction': direction}


async def fetch_btc_history(client: httpx.AsyncClient, start_ms: int, end_ms: int) -> list[float]:
    """Daily closes between [start_ms, end_ms]."""
    r = await client.get(BTC_KLINES_URL, params={
        'symbol': 'BTCUSDT', 'interval': '1d',
        'startTime': start_ms, 'endTime': end_ms, 'limit': 1000
    })
    r.raise_for_status()
    return [float(row[4]) for row in r.json()]  # close


def gbm_probability_above(spot: float, threshold: float, mu: float, sigma: float, T: float) -> float:
    """P(S_T > threshold | spot, mu, sigma, time_years T) under GBM.

    log(S_T) ~ N(log(spot) + (mu - 0.5*sigma^2)*T, sigma^2 * T)
    """
    if T <= 0 or sigma <= 0:
        return 1.0 if spot > threshold else 0.0
    drift_term = (mu - 0.5 * sigma**2) * T
    vol_term = sigma * math.sqrt(T)
    d = (math.log(spot / threshold) + drift_term) / vol_term
    # P(S_T > K) = N(d)
    return 0.5 * (1.0 + math.erf(d / math.sqrt(2)))


async def main():
    t0 = time.time()

    # Fetch resolved BTC-threshold markets с price snapshot data
    # Use canonical price_snapshots for accurate yes_price at decision point
    pg = await asyncpg.connect(_get_db_dsn().replace('postgres:5432', '172.18.0.2:5432'))

    rows = await pg.fetch("""
      SELECT polymarket_id, question, current_price, winner, end_date
        FROM markets
       WHERE resolved = true
         AND winner IS NOT NULL
         AND end_date IS NOT NULL
         AND (question ILIKE '%bitcoin%' OR question ILIKE '% btc %' OR question ILIKE 'btc %')
       ORDER BY end_date DESC
    """)

    print(f"Resolved BTC markets in DB: {len(rows)}")

    parsed = []
    for r in rows:
        p = parse_btc_market(r['question'])
        if p is None: continue
        parsed.append({
            **p,
            'question': r['question'],
            'winner': r['winner'],
            'end_date': r['end_date'],
        })

    print(f"Parsed BTC threshold markets: {len(parsed)}")

    if len(parsed) < 30:
        print("Too few markets parsed for backtest")
        print("Sample of parsed:")
        for p in parsed[:10]:
            print(f"  {p['direction']} ${p['threshold']:.0f} → winner={p['winner']}: {p['question']}")
        return

    # Fetch BTC history covering all parsed end_dates
    min_date = min(p['end_date'] for p in parsed) - timedelta(days=40)
    max_date = max(p['end_date'] for p in parsed) + timedelta(days=1)

    async with httpx.AsyncClient() as client:
        # Fetch in chunks of 1000 days
        all_closes = {}  # date_iso → close
        cur = min_date
        while cur < max_date:
            chunk_end = min(cur + timedelta(days=999), max_date)
            closes = await fetch_btc_history(client,
                int(cur.timestamp() * 1000), int(chunk_end.timestamp() * 1000))
            for i, c in enumerate(closes):
                d = (cur + timedelta(days=i)).date()
                all_closes[d] = c
            cur = chunk_end + timedelta(days=1)

    print(f"BTC history fetched: {len(all_closes)} days")

    # For each market, compute true_p vs market_p (we don't have market_p at T-24h easily, use current_price as last known)
    # Use spot 24h before end_date, sigma from 30d returns, T = 24h

    correct = 0; total = 0
    edges = []
    for p in parsed:
        end_date = p['end_date'].date()
        decision_date = end_date - timedelta(days=1)
        spot = all_closes.get(decision_date)
        if spot is None: continue

        # 30d realized vol
        prior_closes = []
        for k in range(30, 0, -1):
            v = all_closes.get(decision_date - timedelta(days=k))
            if v is not None:
                prior_closes.append(v)
        if len(prior_closes) < 20: continue

        # log returns
        logrets = [math.log(prior_closes[i+1]/prior_closes[i]) for i in range(len(prior_closes)-1)]
        if not logrets: continue
        mean_ret = sum(logrets)/len(logrets)
        var = sum((r - mean_ret)**2 for r in logrets) / len(logrets)
        daily_sigma = math.sqrt(var)

        # GBM probability above threshold at end_date (24h horizon)
        T = 1/365.0  # 1 day in years
        # Annualized: sigma_annual = daily_sigma * sqrt(365), mu_annual = mean_ret*365
        sigma_annual = daily_sigma * math.sqrt(365)
        mu_annual = mean_ret * 365

        p_above = gbm_probability_above(spot, p['threshold'], mu_annual, sigma_annual, T)
        true_p = p_above if p['direction'] == 'above' else (1.0 - p_above)

        actual = 1.0 if p['winner'] == 'yes' else 0.0
        # Prediction correctness
        pred_yes = true_p > 0.5
        if (pred_yes and actual == 1) or (not pred_yes and actual == 0):
            correct += 1
        total += 1
        edges.append({'true_p': true_p, 'actual': actual, 'dir': p['direction'], 'threshold': p['threshold'], 'spot': spot})

    print(f"\nBacktest: {total} markets parsed with BTC history")
    if total == 0:
        await pg.close(); return
    print(f"Naive WR (true_p > 0.5 predicts winner): {correct/total:.1%}")

    # Brier score
    brier = sum((e['true_p'] - e['actual'])**2 for e in edges) / total
    print(f"Brier score: {brier:.4f}")

    # Distribution by true_p bin
    print(f"\nCalibration (true_p bin → actual_WR):")
    bins = [(0, 0.1), (0.1, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 0.9), (0.9, 1.0)]
    for lo, hi in bins:
        subset = [e for e in edges if lo <= e['true_p'] < hi]
        if not subset: continue
        wr = sum(e['actual'] for e in subset) / len(subset)
        avg_tp = sum(e['true_p'] for e in subset) / len(subset)
        print(f"  [{lo:.1f}, {hi:.1f}): n={len(subset):>3} avg_true_p={avg_tp:.3f} actual_WR={wr:.3f}")

    await pg.close()
    print(f"\nElapsed: {time.time()-t0:.1f}s")

if __name__ == "__main__":
    asyncio.run(main())
