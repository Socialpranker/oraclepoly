#!/usr/bin/env python3
"""E1 v2: BTC mirror — без external pip deps. Использует urllib + stdin CSV."""
from __future__ import annotations
import sys, csv, re, math, urllib.request, json, time
from datetime import datetime, timezone, timedelta

BTC_URL = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=1000&startTime={start}&endTime={end}"

ABOVE = ['above', 'over', 'higher than', 'greater than', 'reach', 'hit', 'be at']
BELOW = ['below', 'under', 'less than', 'lower than', 'dip to']


def parse(q: str):
    ql = q.lower()
    if not ('bitcoin' in ql or 'btc' in ql): return None
    direction = None
    for p in ABOVE:
        if p in ql:
            direction = 'above'; break
    if direction is None:
        for p in BELOW:
            if p in ql:
                direction = 'below'; break
    if direction is None: return None
    # Find any number "$X" or "Xk"
    nums = re.findall(r'\$?([\d,]+)([kK])?', q)
    threshold = None
    for val, k in nums:
        try:
            v = float(val.replace(',', ''))
            if k: v *= 1000
            if 10000 <= v <= 1_000_000:
                threshold = v; break
        except ValueError: pass
    if threshold is None: return None
    return {'direction': direction, 'threshold': threshold}


def gbm_p_above(spot, K, mu, sigma, T):
    if T <= 0 or sigma <= 0: return 1.0 if spot > K else 0.0
    d = (math.log(spot/K) + (mu - 0.5*sigma**2)*T) / (sigma * math.sqrt(T))
    return 0.5 * (1.0 + math.erf(d / math.sqrt(2)))


def fetch_btc_chunk(start_ms, end_ms):
    url = BTC_URL.format(start=start_ms, end=end_ms)
    req = urllib.request.Request(url, headers={'User-Agent': 'curl/8.0'})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def main():
    t0 = time.time()
    # Read CSV from stdin: polymarket_id,question,winner,end_date_iso
    reader = csv.DictReader(sys.stdin)
    rows = list(reader)
    print(f"Input rows: {len(rows)}")

    parsed = []
    for r in rows:
        p = parse(r['question'])
        if p is None: continue
        try:
            end_dt = datetime.fromisoformat(r['end_date'].replace('Z', '+00:00'))
        except Exception:
            continue
        parsed.append({**p, 'question': r['question'], 'winner': r['winner'], 'end_date': end_dt})

    print(f"Parsed: {len(parsed)}")
    if len(parsed) < 30:
        for p in parsed[:15]:
            print(f"  {p['direction']} ${p['threshold']:.0f} → {p['winner']}: {p['question'][:60]}")
        return

    # Fetch BTC daily history covering all parsed.end_date - 40 days до max+1
    min_date = min(p['end_date'] for p in parsed) - timedelta(days=40)
    max_date = max(p['end_date'] for p in parsed) + timedelta(days=1)
    print(f"BTC range: {min_date.date()} → {max_date.date()}")

    all_closes = {}
    cur = min_date
    while cur < max_date:
        chunk_end = min(cur + timedelta(days=999), max_date)
        try:
            data = fetch_btc_chunk(int(cur.timestamp()*1000), int(chunk_end.timestamp()*1000))
        except Exception as e:
            print(f"BTC fetch error: {e}"); return
        for i, row in enumerate(data):
            d = datetime.fromtimestamp(row[0]/1000, tz=timezone.utc).date()
            all_closes[d] = float(row[4])
        cur = chunk_end + timedelta(days=1)

    print(f"BTC closes fetched: {len(all_closes)} days")
    if len(all_closes) < 30:
        print("Not enough BTC history"); return

    # Backtest each market
    edges = []
    for p in parsed:
        end_date = p['end_date'].date()
        decision_date = end_date - timedelta(days=1)
        spot = all_closes.get(decision_date)
        if spot is None: continue

        # 30d log returns
        prior = []
        for k in range(30, 0, -1):
            v = all_closes.get(decision_date - timedelta(days=k))
            if v is not None: prior.append(v)
        if len(prior) < 20: continue
        logrets = [math.log(prior[i+1]/prior[i]) for i in range(len(prior)-1)]
        if not logrets: continue
        mean_r = sum(logrets)/len(logrets)
        var = sum((r-mean_r)**2 for r in logrets) / len(logrets)
        daily_sigma = math.sqrt(var)

        sigma_a = daily_sigma * math.sqrt(365)
        mu_a = mean_r * 365
        T = 1/365.0
        p_above = gbm_p_above(spot, p['threshold'], mu_a, sigma_a, T)
        true_p = p_above if p['direction'] == 'above' else (1.0 - p_above)

        actual = 1.0 if p['winner'] == 'yes' else 0.0
        edges.append({
            'true_p': true_p, 'actual': actual,
            'dir': p['direction'], 'thr': p['threshold'], 'spot': spot,
            'sigma_a': sigma_a,
        })

    n = len(edges)
    print(f"\nBacktest n={n}")
    if n == 0: return

    pred_correct = sum(1 for e in edges if (e['true_p'] > 0.5) == (e['actual'] == 1.0))
    print(f"Naive WR (predict > 0.5): {pred_correct/n:.1%}")
    brier = sum((e['true_p'] - e['actual'])**2 for e in edges) / n
    print(f"Brier score: {brier:.4f}")

    print(f"\nCalibration (true_p bin → actual_WR):")
    for lo, hi in [(0, 0.1), (0.1, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 0.9), (0.9, 1.01)]:
        s = [e for e in edges if lo <= e['true_p'] < hi]
        if not s: continue
        wr = sum(e['actual'] for e in s) / len(s)
        avg = sum(e['true_p'] for e in s) / len(s)
        print(f"  [{lo:.1f}, {hi:.1f}): n={len(s):>3} avg_true_p={avg:.3f} actual_WR={wr:.3f}")

    print(f"\nElapsed: {time.time()-t0:.1f}s")

if __name__ == "__main__":
    main()
