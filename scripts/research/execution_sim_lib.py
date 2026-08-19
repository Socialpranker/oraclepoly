"""Reusable execution simulator для Stage 2.5 / Stage 3 любого детектора.

Воспроизводит наш живой execution mechanism:
  - bankroll lookup (shadow $1000 / live $500)
  - EV-aware sizing (fractional Kelly с защитой)
  - hard ceiling entry_price >= 0.95
  - min net edge gate (after fee + spread + buffer)
  - max stake = 0.5% bankroll
  - один сигнал на маркет (FIRST trade в trigger window)

True outcome берём из `markets.outcome_prices` (source of truth), НЕ из enriched
(там coverage 219%, дубликаты, сломанные labels).

Использование:
    from execution_sim_lib import run_simulation, DetectorSpec

    spec = DetectorSpec(
        name='D7',
        signal_sql=...,  # custom SQL для генерации сигналов
        confidence_fn=lambda row: ...,  # cold-start p_calibrated
        side_fn=lambda row: ...,  # 'YES' или 'NO'
    )
    result = run_simulation(spec, since_date='2026-02-01')
    print(result.summary())
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable

import duckdb

# ── Paths (VPS) ──────────────────────────────────────────────────────────────

QUANT_PATH    = "/mnt/data/hf_data/quant.parquet"
MARKETS_PATH  = "/mnt/data/polymarket_dataset/markets.parquet"
ENRICHED_PATH = "/mnt/data/polymarket_dataset/full_hf_enriched.parquet"

# ── Execution constants (mirror app/calibration/ev_sizer.py + sizing_controller.py)
SHADOW_BANKROLL_USD = 1000.0
LIVE_BANKROLL_USD = 500.0
FRACTIONAL_KELLY_COLD = 0.10
MIN_EDGE_PCT = 5.0
EDGE_BUFFER_PCT = 2.0
TOTAL_EDGE_THRESHOLD = MIN_EDGE_PCT + EDGE_BUFFER_PCT
ENTRY_PRICE_CEILING = 0.95
MAX_STAKE_PCT_OF_BANKROLL = 0.005

# Fee + spread by category (estimated, real Polymarket dynamic fee ~1-2% + spread)
DEFAULT_FEE_PCT = 1.5  # per leg
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


# ── Outcome parser ───────────────────────────────────────────────────────────

def parse_outcome(outcome_str: str | None) -> int | None:
    """outcome_prices ['1','0'] → 1 (YES won), ['0','1'] → 0 (NO won), partial → None."""
    if outcome_str is None:
        return None
    s = outcome_str.strip().replace('"', "'")
    if s in ("['1', '0']", "['1.0', '0.0']"):
        return 1
    if s in ("['0', '1']", "['0.0', '1.0']"):
        return 0
    if s.startswith("[") and "," in s:
        try:
            parts = s.strip("[]").split(",")
            yes_p = float(parts[0].strip().strip("'\""))
            no_p = float(parts[1].strip().strip("'\""))
            if yes_p > 0.95 and no_p < 0.05:
                return 1
            if no_p > 0.95 and yes_p < 0.05:
                return 0
        except (ValueError, IndexError):
            pass
    return None


# ── EV sizer (mirror app/calibration/ev_sizer.py) ────────────────────────────

def kelly_fraction(p: float, payout: float) -> float:
    if p <= 0 or p >= 1 or payout <= 0:
        return 0.0
    return (p * payout - (1.0 - p)) / payout


def cost_drag_pct(category: str | None) -> tuple[float, float]:
    fee_drag = DEFAULT_FEE_PCT * 2  # entry + exit
    spread_drag = DEFAULT_SPREAD_PCT_BY_CATEGORY.get(category, 2.5)
    return fee_drag, spread_drag


def ev_sizer_decision(
    cost_per_share: float,
    bankroll_usd: float,
    category: str | None,
    p_calibrated: float,
    fractional_kelly: float = FRACTIONAL_KELLY_COLD,
) -> dict:
    """Решение о sizing. cost_per_share = entry_price (для YES) или 1-entry_price (для NO).

    Returns: {stake, reason, ev_pct, ...}.
    """
    if not (0.0 < p_calibrated < 1.0):
        return {'stake': None, 'reason': 'invalid_p', 'ev_pct': 0}
    if not (0.001 < cost_per_share < 0.999):
        return {'stake': None, 'reason': 'invalid_cost', 'ev_pct': 0}
    if bankroll_usd <= 0:
        return {'stake': None, 'reason': 'no_bankroll', 'ev_pct': 0}
    if cost_per_share >= ENTRY_PRICE_CEILING:
        return {'stake': None, 'reason': f'cost_ceiling', 'ev_pct': 0}

    payout = 1.0 / cost_per_share
    ev_per_dollar = p_calibrated * payout - (1.0 - p_calibrated)
    ev_pct = ev_per_dollar * 100.0

    fee_drag, spread_drag = cost_drag_pct(category)
    net_ev_pct = ev_pct - fee_drag - spread_drag

    if net_ev_pct < TOTAL_EDGE_THRESHOLD:
        return {'stake': None, 'reason': f'ev_below_thr', 'ev_pct': net_ev_pct,
                'gross_ev_pct': ev_pct}

    f_star = kelly_fraction(p_calibrated, payout)
    if f_star <= 0:
        return {'stake': None, 'reason': 'negative_kelly', 'ev_pct': net_ev_pct}

    raw_stake = bankroll_usd * fractional_kelly * f_star
    cap = bankroll_usd * MAX_STAKE_PCT_OF_BANKROLL
    stake = min(raw_stake, cap)

    return {
        'stake': round(stake, 2),
        'reason': 'ok',
        'ev_pct': net_ev_pct,
        'gross_ev_pct': ev_pct,
        'cost': cost_per_share,
        'payout': payout,
    }


# ── Detector spec ────────────────────────────────────────────────────────────

@dataclass
class DetectorSpec:
    """Описание одного детектора для симуляции.

    Attributes:
        name: human-readable name (e.g. 'D7', 'D8_FOMC')
        signal_sql: DuckDB SQL который возвращает один row на (condition_id) с колонками:
                    condition_id, end_date, entry_ts, entry_price, volume,
                    category, outcome_prices.
                    Передаётся string без подстановок (использует ${QUANT_PATH} плейсхолдеры).
        side_fn: row -> 'YES' | 'NO' | None (skip)
        confidence_fn: row -> p_calibrated в [0,1] (cold-start prior)
    """
    name: str
    signal_sql: str
    side_fn: Callable[[dict], str | None]
    confidence_fn: Callable[[dict], float]
    description: str = ""
    fractional_kelly: float = FRACTIONAL_KELLY_COLD


@dataclass
class SimulationResult:
    detector_name: str
    period_start: date
    period_end: date
    n_days: int
    n_signals: int
    n_trades: int
    n_won: int
    n_skipped: int
    n_invalid_outcome: int
    skip_reasons: dict[str, int]
    total_stake: float
    total_gross_pnl: float
    total_fee_spread: float
    total_net_pnl: float
    trades: list[dict]
    elapsed_sec: float

    @property
    def daily_net_pnl(self) -> float:
        return self.total_net_pnl / max(self.n_days, 1)

    @property
    def win_rate(self) -> float:
        return self.n_won / max(self.n_trades, 1)

    @property
    def roi_on_stake(self) -> float:
        return self.total_net_pnl / max(self.total_stake, 0.01)

    @property
    def trades_per_day(self) -> float:
        return self.n_trades / max(self.n_days, 1)

    @property
    def verdict(self) -> str:
        d = self.daily_net_pnl
        if d > 20:
            return "✅ PASS"
        if d > 5:
            return "⚠️ MARGINAL"
        return "❌ FAIL"

    def summary(self) -> str:
        return (
            f"{self.detector_name}: {self.verdict} | "
            f"{self.n_trades:,} trades, WR {self.win_rate:.1%}, "
            f"net ${self.total_net_pnl:+,.0f} ({self.roi_on_stake*100:+.1f}% ROI), "
            f"daily ${self.daily_net_pnl:+.2f}"
        )


def run_simulation(
    spec: DetectorSpec,
    since: str = "2026-02-01",
    bankroll: float = SHADOW_BANKROLL_USD,
    threads: int = 4,
    memory_limit: str = "6GB",
    verbose: bool = True,
) -> SimulationResult:
    """Запустить симуляцию execution для одного детектора."""
    t0 = time.time()
    since_ts = int(datetime.fromisoformat(since).timestamp())

    if verbose:
        print(f"[{spec.name}] Connecting DuckDB...", flush=True)
    con = duckdb.connect()
    con.execute(f'SET memory_limit="{memory_limit}"')
    con.execute(f'SET threads={threads}')

    if verbose:
        print(f"[{spec.name}] Querying signals (since {since})...", flush=True)
    sql = spec.signal_sql.format(
        QUANT_PATH=QUANT_PATH,
        MARKETS_PATH=MARKETS_PATH,
        ENRICHED_PATH=ENRICHED_PATH,
        SINCE_TIMESTAMP=since_ts,
    )
    rows = con.execute(sql).fetchall()
    cols = [d[0] for d in con.description]
    signals = [dict(zip(cols, r)) for r in rows]
    if verbose:
        print(f"[{spec.name}]   → {len(signals):,} signals", flush=True)

    trades = []
    skip_reasons: dict[str, int] = {}
    invalid_outcome = 0

    for sig in signals:
        side = spec.side_fn(sig)
        if side is None:
            skip_reasons['no_side'] = skip_reasons.get('no_side', 0) + 1
            continue

        entry_price = float(sig['entry_price'])
        cost = entry_price if side == 'YES' else 1.0 - entry_price

        p_cal = spec.confidence_fn(sig)
        if not (0 < p_cal < 1):
            skip_reasons['invalid_confidence'] = skip_reasons.get('invalid_confidence', 0) + 1
            continue

        decision = ev_sizer_decision(
            cost_per_share=cost,
            bankroll_usd=bankroll,
            category=sig.get('category'),
            p_calibrated=p_cal,
            fractional_kelly=spec.fractional_kelly,
        )

        if decision['stake'] is None:
            r = decision['reason']
            skip_reasons[r] = skip_reasons.get(r, 0) + 1
            continue

        outcome = parse_outcome(sig['outcome_prices'])
        if outcome is None:
            invalid_outcome += 1
            continue

        stake = decision['stake']
        shares = stake / cost
        won = (side == 'YES' and outcome == 1) or (side == 'NO' and outcome == 0)
        gross_payout = shares if won else 0.0
        gross_pnl = gross_payout - stake

        fee = (DEFAULT_FEE_PCT / 100.0) * 2
        spread = DEFAULT_SPREAD_PCT_BY_CATEGORY.get(sig.get('category'), 2.5) / 100.0
        cost_total = stake * (fee + spread)
        net_pnl = gross_pnl - cost_total

        trades.append({
            'condition_id': sig['condition_id'],
            'category': sig.get('category'),
            'side': side,
            'entry_price': entry_price,
            'cost_per_share': cost,
            'stake': stake,
            'outcome_yes_won': outcome,
            'won': won,
            'gross_pnl': gross_pnl,
            'fee_spread_cost': cost_total,
            'net_pnl': net_pnl,
            'end_date': sig['end_date'],
            'p_calibrated': p_cal,
            'ev_pct': decision['ev_pct'],
        })

    elapsed = time.time() - t0

    if not trades:
        # Edge case: no trades survived gates
        return SimulationResult(
            detector_name=spec.name,
            period_start=date.today(),
            period_end=date.today(),
            n_days=1,
            n_signals=len(signals),
            n_trades=0,
            n_won=0,
            n_skipped=sum(skip_reasons.values()),
            n_invalid_outcome=invalid_outcome,
            skip_reasons=skip_reasons,
            total_stake=0.0,
            total_gross_pnl=0.0,
            total_fee_spread=0.0,
            total_net_pnl=0.0,
            trades=[],
            elapsed_sec=elapsed,
        )

    days = sorted(set(t['end_date'].date() for t in trades))
    n_days = (max(days) - min(days)).days + 1

    return SimulationResult(
        detector_name=spec.name,
        period_start=min(days),
        period_end=max(days),
        n_days=n_days,
        n_signals=len(signals),
        n_trades=len(trades),
        n_won=sum(1 for t in trades if t['won']),
        n_skipped=sum(skip_reasons.values()),
        n_invalid_outcome=invalid_outcome,
        skip_reasons=skip_reasons,
        total_stake=sum(t['stake'] for t in trades),
        total_gross_pnl=sum(t['gross_pnl'] for t in trades),
        total_fee_spread=sum(t['fee_spread_cost'] for t in trades),
        total_net_pnl=sum(t['net_pnl'] for t in trades),
        trades=trades,
        elapsed_sec=elapsed,
    )


def render_report(result: SimulationResult) -> str:
    """Standardized markdown report for products/<detector>/execution_simulation.md."""
    lines = [
        f"# {result.detector_name} Execution Simulation",
        "",
        f"**Дата:** {date.today().isoformat()}  ",
        f"**Период:** {result.period_start} → {result.period_end} ({result.n_days} days)  ",
        f"**Bankroll:** ${SHADOW_BANKROLL_USD:.0f} (shadow)  ",
        f"**Sizing:** EV-aware fractional Kelly (f={FRACTIONAL_KELLY_COLD}), "
        f"max stake {MAX_STAKE_PCT_OF_BANKROLL*100:.1f}% bankroll  ",
        f"**Min net edge:** {TOTAL_EDGE_THRESHOLD}%  ",
        f"**Outcome source:** `markets.outcome_prices` (source of truth)  ",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Candidate signals | {result.n_signals:,} |",
        f"| Trades executed | {result.n_trades:,} ({result.n_trades/max(result.n_signals,1)*100:.1f}%) |",
        f"| Win rate | {result.win_rate:.1%} |",
        f"| Total stake | ${result.total_stake:,.2f} |",
        f"| Gross PnL | ${result.total_gross_pnl:+,.2f} |",
        f"| Fee+spread | ${result.total_fee_spread:,.2f} |",
        f"| **Net PnL** | **${result.total_net_pnl:+,.2f}** |",
        f"| ROI on stake | {result.roi_on_stake*100:+.1f}% |",
        f"| **Daily Net PnL** | **${result.daily_net_pnl:+.2f}** |",
        f"| Trades/day | {result.trades_per_day:.1f} |",
        "",
        "## Skip reasons",
        "",
    ]
    for r, c in sorted(result.skip_reasons.items(), key=lambda x: -x[1])[:15]:
        lines.append(f"- `{r}`: {c:,}")
    lines.append(f"- `invalid_outcome` (partial resolution): {result.n_invalid_outcome:,}")
    lines.append("")

    # Per-side
    if result.trades:
        lines.append("## Per-side")
        lines.append("")
        lines.append("| Side | n | WR | stake | gross | fee | net | ROI |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for side in ['YES', 'NO']:
            ts = [t for t in result.trades if t['side'] == side]
            if not ts:
                continue
            n = len(ts)
            won = sum(1 for t in ts if t['won'])
            stake = sum(t['stake'] for t in ts)
            gross = sum(t['gross_pnl'] for t in ts)
            fee = sum(t['fee_spread_cost'] for t in ts)
            net = sum(t['net_pnl'] for t in ts)
            roi = net / stake * 100 if stake > 0 else 0
            lines.append(f"| {side} | {n:,} | {won/n:.1%} | ${stake:.0f} | "
                         f"${gross:+.0f} | ${fee:.0f} | ${net:+.0f} | {roi:+.1f}% |")
        lines.append("")

        # Per-category
        lines.append("## Per-category × side")
        lines.append("")
        lines.append("| Category | Side | n | WR | net_PnL | ROI |")
        lines.append("|---|---|---|---|---|---|")
        cats: dict = {}
        for t in result.trades:
            key = (t['category'], t['side'])
            if key not in cats:
                cats[key] = {'n': 0, 'won': 0, 'stake': 0, 'net': 0}
            cats[key]['n'] += 1
            cats[key]['won'] += int(t['won'])
            cats[key]['stake'] += t['stake']
            cats[key]['net'] += t['net_pnl']
        for (cat, side), s in sorted(cats.items(), key=lambda x: -x[1]['net']):
            wr = s['won']/s['n']
            roi = s['net']/s['stake']*100 if s['stake'] else 0
            lines.append(f"| {cat or 'None'} | {side} | {s['n']:,} | {wr:.1%} | "
                         f"${s['net']:+.0f} | {roi:+.1f}% |")
        lines.append("")

    # Verdict
    lines.append("## Verdict")
    lines.append("")
    lines.append(f"- **Daily Net PnL:** ${result.daily_net_pnl:+.2f}")
    lines.append(f"- **Trades/day:** {result.trades_per_day:.1f}")
    lines.append(f"- **Avg stake:** ${result.total_stake/max(result.n_trades,1):.2f}")
    lines.append("")
    lines.append(f"**{result.verdict}** — per detector_lifecycle.md Stage 3 thresholds:")
    lines.append("")
    lines.append("- ✅ PASS если daily PnL > $20 → shadow с stake $5")
    lines.append("- ⚠️ MARGINAL если $5-20 → продолжать backtest")
    lines.append("- ❌ FAIL если < $5 → отказ")
    lines.append("")
    lines.append("---")
    lines.append(f"*Simulation completed in {result.elapsed_sec:.1f}s*")

    return "\n".join(lines)


def save_report(result: SimulationResult, slug: str, base_dir: Path | None = None) -> Path:
    """Save report to products/<slug>/execution_simulation.md."""
    if base_dir is None:
        base_dir = Path(__file__).parent.parent.parent / "products"
    out_dir = base_dir / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "execution_simulation.md"
    out_path.write_text(render_report(result))
    return out_path
