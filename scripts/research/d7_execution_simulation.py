#!/usr/bin/env python3
"""D7 Execution Simulation — как бы наш bot реально торговал на HF data.

Использует true outcome из markets.outcome_prices (НЕ enriched.win, который
имеет 219% coverage и сломанные labels).

Симулирует execution mechanism:
  - Bankroll: $500 (live) / $1000 (shadow)
  - Sizing: ev_sizer с FRACTIONAL_KELLY_COLD = 0.10 (cold-start)
  - Min edge gate: net_ev > 7% (после fee + spread + 2% buffer)
  - Hard ceiling: entry_price < 0.95
  - Max stake: bankroll × 0.5%
  - Один сигнал на маркет — first trade в окне 4-72h до резолва

Trigger condition (D7): YES @ 0.05-0.15 OR NO @ 0.85-0.95, vol >= $5K, 4-72h window.

Detector confidence (raw): для D7 confidence = 1 - entry_price для NO,
                          и (1 - entry_price) - 0.5 = "edge expectation" for YES.
                          В cold-start confidence = empirical WR в bucket из v3 backtest.

Outcome: outcome_prices = ['1','0'] → YES won; ['0','1'] → NO won.
         Partial outcomes (e.g. ['0.5','0.5']) исключаем.

Запуск:
    cd ~/oracle && python3 scripts/research/d7_execution_simulation.py
"""

from __future__ import annotations

import sys
import time
from datetime import date, datetime
from pathlib import Path

import duckdb

# ── Constants ────────────────────────────────────────────────────────────────

QUANT_PATH    = "/mnt/data/hf_data/quant.parquet"
MARKETS_PATH  = "/mnt/data/polymarket_dataset/markets.parquet"
ENRICHED_PATH = "/mnt/data/polymarket_dataset/full_hf_enriched.parquet"

VOLUME_FLOOR_USD = 5_000
HOURS_MIN, HOURS_MAX = 4, 72

NO_ZONE_LOW, NO_ZONE_HIGH = 0.85, 0.95
YES_ZONE_LOW, YES_ZONE_HIGH = 0.05, 0.15

SINCE_TIMESTAMP = int(datetime(2026, 2, 1).timestamp())

# Execution constants from app/calibration/ev_sizer.py + sizing_controller.py
SHADOW_BANKROLL_USD = 1000.0
LIVE_BANKROLL_USD = 500.0
FRACTIONAL_KELLY_COLD = 0.10
MIN_EDGE_PCT = 5.0
EDGE_BUFFER_PCT = 2.0  # extra гарантия
TOTAL_EDGE_THRESHOLD = MIN_EDGE_PCT + EDGE_BUFFER_PCT  # 7%
ENTRY_PRICE_CEILING = 0.95
MAX_STAKE_PCT_OF_BANKROLL = 0.005  # 0.5%

# Polymarket fees by category (estimated, real dynamic ~1-2%)
DEFAULT_FEE_PCT = 1.5
DEFAULT_SPREAD_PCT_BY_CATEGORY = {
    "Crypto": 1.0,
    "Politics": 1.5,
    "Sports": 1.0,
    "Tech & AI": 2.0,
    "Weather": 3.0,
    "Geopolitics": 2.5,
    "Other": 2.0,
    "Culture": 2.0,
    "Finance": 1.5,
    "Minute Markets": 4.0,
    None: 2.5,
}

# ── Detector confidence: empirical WR per bucket (from v3 backtest if available)
# These are fallback values if we don't have ML calibration. In practice cold-start.
DETECTOR_CONFIDENCE = {
    ('YES', 0.05, 0.10): 0.367,  # backtest v3 WR
    ('YES', 0.10, 0.15): 0.440,
    ('NO',  0.85, 0.90): 0.159,
    ('NO',  0.90, 0.95): 0.137,
}


def kelly_fraction(p: float, payout: float) -> float:
    """f* = (p*b - q) / b, b = payout"""
    if p <= 0 or p >= 1 or payout <= 0:
        return 0.0
    q = 1.0 - p
    return (p * payout - q) / payout


def cost_drag_pct(entry_price: float, category: str | None) -> tuple[float, float]:
    """Returns (fee_drag_pct, spread_drag_pct) per $1 stake."""
    fee_drag = DEFAULT_FEE_PCT * 2  # entry + exit
    spread_drag = DEFAULT_SPREAD_PCT_BY_CATEGORY.get(category, 2.5)
    return fee_drag, spread_drag


def ev_sizer_decision(
    entry_price: float,
    side: str,
    bankroll_usd: float,
    category: str | None,
    p_calibrated: float,
) -> dict:
    """Замена нашего ev_sizer.size_from_calibrated_p.

    Returns dict with stake_usd or None (skip), reason.
    """
    # Compute effective price for the side we're buying:
    # YES side → buy at entry_price (yes_price)
    # NO side  → buy at (1 - entry_price)
    if side == 'YES':
        cost_per_share = entry_price
    else:  # NO
        cost_per_share = 1.0 - entry_price

    if not (0.0 < p_calibrated < 1.0):
        return {'stake': None, 'reason': 'invalid_p', 'ev_pct': 0}
    if not (0.001 < cost_per_share < 0.999):
        return {'stake': None, 'reason': 'invalid_cost', 'ev_pct': 0}
    if bankroll_usd <= 0:
        return {'stake': None, 'reason': 'no_bankroll', 'ev_pct': 0}

    # Hard gate: cost ceiling (analog ENTRY_PRICE_CEILING)
    if cost_per_share >= ENTRY_PRICE_CEILING:
        return {'stake': None, 'reason': f'cost_ceiling_{cost_per_share:.3f}', 'ev_pct': 0}

    # Payout: $1 per winning share
    payout = 1.0 / cost_per_share

    # Gross EV per $1 stake
    ev_per_dollar = p_calibrated * payout - (1.0 - p_calibrated)
    ev_pct = ev_per_dollar * 100.0

    # Net EV after costs
    fee_drag, spread_drag = cost_drag_pct(cost_per_share, category)
    net_ev_pct = ev_pct - fee_drag - spread_drag

    if net_ev_pct < TOTAL_EDGE_THRESHOLD:
        return {'stake': None, 'reason': f'ev_below_thr_{net_ev_pct:.1f}',
                'ev_pct': net_ev_pct, 'gross_ev_pct': ev_pct}

    f_star = kelly_fraction(p_calibrated, payout)
    if f_star <= 0:
        return {'stake': None, 'reason': 'negative_kelly', 'ev_pct': net_ev_pct}

    raw_stake = bankroll_usd * FRACTIONAL_KELLY_COLD * f_star
    cap = bankroll_usd * MAX_STAKE_PCT_OF_BANKROLL
    stake = min(raw_stake, cap)

    return {
        'stake': round(stake, 2),
        'reason': 'ok',
        'ev_pct': net_ev_pct,
        'gross_ev_pct': ev_pct,
        'f_star': f_star,
        'cost': cost_per_share,
        'payout': payout,
    }


# ── SQL: signal generation ───────────────────────────────────────────────────

SIGNALS_SQL = f"""
WITH first_trade_in_window AS (
    SELECT
        q.condition_id,
        m.end_date,
        m.volume,
        m.outcome_prices,
        e.category,
        FIRST_VALUE(q.timestamp) OVER (
            PARTITION BY q.condition_id ORDER BY q.timestamp
        ) AS entry_ts,
        FIRST_VALUE(q.price) OVER (
            PARTITION BY q.condition_id ORDER BY q.timestamp
        ) AS entry_price
    FROM read_parquet('{QUANT_PATH}') q
    JOIN read_parquet('{MARKETS_PATH}') m ON m.condition_id = q.condition_id
    LEFT JOIN read_parquet('{ENRICHED_PATH}') e ON e.market_id = q.condition_id
    WHERE q.timestamp >= {SINCE_TIMESTAMP}
      AND m.end_date IS NOT NULL
      AND m.closed = 1
      AND m.volume >= {VOLUME_FLOOR_USD}
      AND m.outcome_prices IS NOT NULL
      AND ((q.price BETWEEN {NO_ZONE_LOW} AND {NO_ZONE_HIGH})
           OR (q.price BETWEEN {YES_ZONE_LOW} AND {YES_ZONE_HIGH}))
      AND (EXTRACT(EPOCH FROM m.end_date)::BIGINT - q.timestamp)/3600.0
            BETWEEN {HOURS_MIN} AND {HOURS_MAX}
)
SELECT DISTINCT
    condition_id,
    end_date,
    entry_ts,
    entry_price,
    volume,
    category,
    outcome_prices
FROM first_trade_in_window
"""


def parse_outcome(outcome_str: str) -> int | None:
    """outcome_prices like ['1', '0'] → 1 (YES won), ['0', '1'] → 0 (NO won).
    Partial outcomes (0.5/0.5 etc.) → None (skip).
    Returns: 1=YES won, 0=NO won, None=invalid.
    """
    if outcome_str is None:
        return None
    s = outcome_str.strip().replace('"', "'")
    # Common cases
    if s == "['1', '0']" or s == "['1.0', '0.0']":
        return 1  # YES won
    if s == "['0', '1']" or s == "['0.0', '1.0']":
        return 0  # NO won
    # Decimal cases like ['0.0005', '0.9995']
    if s.startswith("[") and "," in s:
        try:
            parts = s.strip("[]").split(",")
            yes_p = float(parts[0].strip().strip("'\""))
            no_p = float(parts[1].strip().strip("'\""))
            # Strict: only definitive outcomes
            if yes_p > 0.95 and no_p < 0.05:
                return 1
            if no_p > 0.95 and yes_p < 0.05:
                return 0
            return None  # ambiguous
        except (ValueError, IndexError):
            return None
    return None


def get_confidence(side: str, entry_price: float) -> float:
    """Cold-start confidence based on price bucket → empirical WR from v3 backtest."""
    if side == 'YES':
        if YES_ZONE_LOW <= entry_price < 0.10:
            return DETECTOR_CONFIDENCE[('YES', 0.05, 0.10)]
        if 0.10 <= entry_price <= YES_ZONE_HIGH:
            return DETECTOR_CONFIDENCE[('YES', 0.10, 0.15)]
    else:  # NO
        if NO_ZONE_LOW <= entry_price < 0.90:
            return DETECTOR_CONFIDENCE[('NO', 0.85, 0.90)]
        if 0.90 <= entry_price <= NO_ZONE_HIGH:
            return DETECTOR_CONFIDENCE[('NO', 0.90, 0.95)]
    return 0.0


def main():
    t0 = time.time()
    print("Connecting DuckDB...", flush=True)
    con = duckdb.connect()
    con.execute('SET memory_limit="6GB"')
    con.execute('SET threads=4')

    print(f"Querying signals (since {datetime.fromtimestamp(SINCE_TIMESTAMP).date()})...", flush=True)
    rows = con.execute(SIGNALS_SQL).fetchall()
    cols = [d[0] for d in con.description]
    signals = [dict(zip(cols, r)) for r in rows]
    print(f"  → {len(signals):,} candidate signals", flush=True)

    # ── Simulate execution ──
    print("Simulating execution (shadow bankroll $1000)...", flush=True)
    bankroll = SHADOW_BANKROLL_USD

    trades = []
    skip_reasons = {}
    invalid_outcome = 0

    for sig in signals:
        entry_price = float(sig['entry_price'])
        category = sig['category']

        # Determine side based on price zone
        if YES_ZONE_LOW <= entry_price <= YES_ZONE_HIGH:
            side = 'YES'
        elif NO_ZONE_LOW <= entry_price <= NO_ZONE_HIGH:
            side = 'NO'
        else:
            continue

        # Get confidence (cold-start: empirical WR in bucket)
        p_cal = get_confidence(side, entry_price)
        if p_cal == 0.0:
            continue

        # Run sizer
        decision = ev_sizer_decision(
            entry_price=entry_price,
            side=side,
            bankroll_usd=bankroll,
            category=category,
            p_calibrated=p_cal,
        )

        if decision['stake'] is None:
            r = decision['reason'].split('_')[0]  # group by short reason
            skip_reasons[r] = skip_reasons.get(r, 0) + 1
            continue

        # Get true outcome
        outcome = parse_outcome(sig['outcome_prices'])
        if outcome is None:
            invalid_outcome += 1
            continue

        # Compute PnL
        stake = decision['stake']
        cost_per_share = entry_price if side == 'YES' else 1.0 - entry_price
        shares = stake / cost_per_share
        won = (side == 'YES' and outcome == 1) or (side == 'NO' and outcome == 0)
        gross_payout = shares if won else 0.0
        gross_pnl = gross_payout - stake

        # Apply fee + spread (real cost)
        fee_drag = DEFAULT_FEE_PCT / 100.0
        spread_drag = DEFAULT_SPREAD_PCT_BY_CATEGORY.get(category, 2.5) / 100.0
        cost = stake * (fee_drag * 2 + spread_drag)  # 2 legs fee + spread on entry
        net_pnl = gross_pnl - cost

        trades.append({
            'condition_id': sig['condition_id'],
            'category': category,
            'side': side,
            'entry_price': entry_price,
            'cost_per_share': cost_per_share,
            'stake': stake,
            'outcome_yes_won': outcome,
            'won': won,
            'gross_payout': gross_payout,
            'gross_pnl': gross_pnl,
            'fee_spread_cost': cost,
            'net_pnl': net_pnl,
            'end_date': sig['end_date'],
        })

    elapsed = time.time() - t0
    print(f"  → {len(trades):,} trades executed (skipped {sum(skip_reasons.values()):,}, "
          f"invalid_outcome {invalid_outcome:,})", flush=True)

    # ── Aggregate ──
    if not trades:
        print("No trades. Skip aggregation.")
        return

    total_stake = sum(t['stake'] for t in trades)
    total_gross_pnl = sum(t['gross_pnl'] for t in trades)
    total_net_pnl = sum(t['net_pnl'] for t in trades)
    n_won = sum(1 for t in trades if t['won'])
    n_total = len(trades)
    total_fee = sum(t['fee_spread_cost'] for t in trades)

    # Days for daily PnL
    days = sorted(set(t['end_date'].date() for t in trades))
    n_days = (max(days) - min(days)).days + 1

    print(f"\n{'='*80}")
    print("D7 EXECUTION SIMULATION RESULTS")
    print(f"{'='*80}\n")
    print(f"Period: {min(days)} → {max(days)} ({n_days} days)")
    print(f"Trades executed: {n_total:,}")
    print(f"Win rate: {n_won/n_total:.1%} ({n_won:,}/{n_total:,})")
    print(f"Total stake deployed: ${total_stake:,.2f}")
    print(f"Total gross PnL: ${total_gross_pnl:,.2f}")
    print(f"Total fee+spread: ${total_fee:,.2f}")
    print(f"Total NET PnL: ${total_net_pnl:,.2f}")
    print(f"ROI on stake: {total_net_pnl/total_stake*100:.1f}%")
    print(f"Daily NET PnL: ${total_net_pnl/n_days:.2f}")
    print(f"Trades/day: {n_total/n_days:.1f}")
    print()

    # Skip reasons
    print(f"Skip reasons (top 10):")
    for r, c in sorted(skip_reasons.items(), key=lambda x: -x[1])[:10]:
        print(f"  {r}: {c:,}")
    print()

    # Per-side breakdown
    print(f"{'Side':5} {'n':>6} {'WR':>6} {'stake':>10} {'gross_pnl':>10} {'net_pnl':>10} {'ROI':>8}")
    print("-" * 60)
    for side in ['YES', 'NO']:
        side_trades = [t for t in trades if t['side'] == side]
        if not side_trades:
            continue
        s_n = len(side_trades)
        s_won = sum(1 for t in side_trades if t['won'])
        s_stake = sum(t['stake'] for t in side_trades)
        s_gross = sum(t['gross_pnl'] for t in side_trades)
        s_net = sum(t['net_pnl'] for t in side_trades)
        s_roi = s_net / s_stake * 100 if s_stake > 0 else 0
        print(f"{side:5} {s_n:>6,} {s_won/s_n:>6.1%} ${s_stake:>9.0f} ${s_gross:>9.0f} ${s_net:>9.0f} {s_roi:>7.1f}%")
    print()

    # Per-category breakdown
    print(f"\n{'Category':16} {'side':5} {'n':>6} {'WR':>6} {'net_pnl':>10} {'ROI':>8}")
    print("-" * 70)
    cats = {}
    for t in trades:
        key = (t['category'], t['side'])
        if key not in cats:
            cats[key] = {'n': 0, 'won': 0, 'stake': 0, 'net': 0}
        cats[key]['n'] += 1
        cats[key]['won'] += int(t['won'])
        cats[key]['stake'] += t['stake']
        cats[key]['net'] += t['net_pnl']

    for (cat, side), s in sorted(cats.items(), key=lambda x: -x[1]['net']):
        wr = s['won']/s['n'] if s['n'] else 0
        roi = s['net']/s['stake']*100 if s['stake'] else 0
        print(f"{str(cat or 'None'):16} {side:5} {s['n']:>6,} {wr:>6.1%} ${s['net']:>9.0f} {roi:>7.1f}%")

    # ── Save report ──
    out_dir = Path(__file__).parent.parent.parent / "products" / "d7-pre-resolution-convergence-fade"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "execution_simulation.md"

    lines = [
        "# D7 Execution Simulation",
        "",
        f"**Дата:** {date.today().isoformat()}  ",
        f"**Период:** {min(days)} → {max(days)} ({n_days} days)  ",
        f"**Bankroll:** ${SHADOW_BANKROLL_USD:.0f} (shadow)  ",
        f"**Sizing:** EV-aware fractional Kelly (f={FRACTIONAL_KELLY_COLD}), max stake {MAX_STAKE_PCT_OF_BANKROLL*100}% bankroll  ",
        f"**Min net edge:** {TOTAL_EDGE_THRESHOLD}% после fee+spread+buffer  ",
        f"**Outcome source:** `markets.outcome_prices` (true source-of-truth)  ",
        "",
        "## Summary",
        "",
        f"| Metric | Value |",
        f"|---|---|",
        f"| Candidate signals | {len(signals):,} |",
        f"| Trades executed | {n_total:,} ({n_total/len(signals)*100:.1f}% of candidates) |",
        f"| Win rate | {n_won/n_total:.1%} |",
        f"| Total stake | ${total_stake:,.2f} |",
        f"| Total gross PnL | ${total_gross_pnl:+,.2f} |",
        f"| Fee+spread cost | ${total_fee:,.2f} |",
        f"| **Total NET PnL** | **${total_net_pnl:+,.2f}** |",
        f"| ROI on stake | {total_net_pnl/total_stake*100:+.1f}% |",
        f"| **Daily NET PnL** | **${total_net_pnl/n_days:+.2f}/day** |",
        f"| Trades/day | {n_total/n_days:.1f} |",
        "",
        "## Skip reasons",
        "",
    ]
    for r, c in sorted(skip_reasons.items(), key=lambda x: -x[1])[:10]:
        lines.append(f"- `{r}`: {c:,}")
    lines.append(f"- `invalid_outcome`: {invalid_outcome:,} (partial/ambiguous resolution)")
    lines.append("")

    lines.extend([
        "## Per-side breakdown",
        "",
        "| Side | n | WR | stake | gross_PnL | fee | net_PnL | ROI |",
        "|---|---|---|---|---|---|---|---|",
    ])
    for side in ['YES', 'NO']:
        side_trades = [t for t in trades if t['side'] == side]
        if not side_trades:
            continue
        s_n = len(side_trades)
        s_won = sum(1 for t in side_trades if t['won'])
        s_stake = sum(t['stake'] for t in side_trades)
        s_gross = sum(t['gross_pnl'] for t in side_trades)
        s_fee = sum(t['fee_spread_cost'] for t in side_trades)
        s_net = sum(t['net_pnl'] for t in side_trades)
        s_roi = s_net / s_stake * 100 if s_stake > 0 else 0
        lines.append(f"| {side} | {s_n:,} | {s_won/s_n:.1%} | ${s_stake:.0f} | ${s_gross:+.0f} | ${s_fee:.0f} | ${s_net:+.0f} | {s_roi:+.1f}% |")
    lines.append("")

    lines.extend([
        "## Per-category breakdown",
        "",
        "| Category | Side | n | WR | net_PnL | ROI |",
        "|---|---|---|---|---|---|",
    ])
    for (cat, side), s in sorted(cats.items(), key=lambda x: -x[1]['net']):
        wr = s['won']/s['n'] if s['n'] else 0
        roi = s['net']/s['stake']*100 if s['stake'] else 0
        lines.append(f"| {str(cat or 'None')} | {side} | {s['n']:,} | {wr:.1%} | ${s['net']:+.0f} | {roi:+.1f}% |")
    lines.append("")

    lines.extend([
        "## Decision (per detector_lifecycle.md Stage 3)",
        "",
        f"- **Daily PnL:** ${total_net_pnl/n_days:+.2f}",
        f"- **Capacity:** ~{n_total/n_days:.0f} trades/day at avg stake ${total_stake/n_total:.2f}",
    ])
    daily_pnl = total_net_pnl / n_days
    if daily_pnl > 20:
        lines.append(f"- **Verdict: ✅ PASS** (>$20/day) → готов к shadow с stake ${total_stake/n_total:.2f}")
    elif daily_pnl > 5:
        lines.append(f"- **Verdict: ⚠️ MARGINAL** ($5-20/day) → продолжать backtest, не shadow")
    else:
        lines.append(f"- **Verdict: ❌ FAIL** (<$5/day) → отказ от детектора")

    lines.append("")
    lines.append(f"---")
    lines.append(f"*Simulation completed in {elapsed:.1f}s*")

    out_path.write_text("\n".join(lines))
    print(f"\nReport saved: {out_path}", flush=True)


if __name__ == "__main__":
    sys.exit(main() or 0)
