"""Exit-trajectories audit (Часть 1 спеки 2026-07-06-exit-trajectories-audit-design.md).

MAE/MFE + counterfactual TP/SL-грид на гранулярных траекториях
detector_signal_price_snapshots. One-off, запускать ТОЛЬКО на проде:

    cd /root/oracle && docker run --rm --env-file .env --memory=4g \\
        --network oracle_backend_net -v /root/oracle:/app -w /app \\
        oracle-backend:latest \\
        python -m scripts.research.exit_trajectories_audit --out-dir research/exit-trajectories-audit-2026-07-06

--memory=4g: bootstrap-пик ~2.4 GB на когорте n≈20K (BOOTSTRAP_RESAMPLES=10_000 ×
len(values) int32-индексы на грид SL×TP×haircut ячейку); рядом на хосте живут
прод-контейнеры, без лимита один прогон может их придушить.

P&L-конвенция — прод-функция _signal_payout_pct_at (shadow_simulation):
side-aware cost-basis, проценты от entry_cost. Haircut: early-exit = 2 ноги
(полный), hold-to-resolution = 1 нога (половина).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.backtest.metrics import wilson_score_interval
from app.services.shadow_simulation import _signal_payout_pct_at

MIN_SNAPSHOTS = 20
MIN_N_PER_DETECTOR = 40  # конвенция m2_forward_analysis: ниже — insufficient n
BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 42

SL_GRID: list[float | None] = [None, -20.0, -30.0, -40.0, -50.0, -60.0, -80.0]
TP_GRID: list[float | None] = [None, 20.0, 30.0, 50.0, 80.0, 100.0]
HAIRCUT_PCTS = [0.0, 3.0, 7.0]
MAE_THRESHOLDS = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 80.0]


@dataclass
class SignalTrajectory:
    signal_id: str
    detector: str
    side: str  # 'YES' | 'NO'
    entry_yes: float
    outcome: str  # 'WIN' | 'LOSS'
    generated_at: datetime
    resolved_at: datetime
    snapshots: list[tuple[datetime, float]]  # (captured_at, yes_price), отсортированы


def compute_mae_mfe(traj: SignalTrajectory) -> tuple[float, float]:
    """(MAE, MFE) в pnl%: минимум и максимум по пути, клампнутые к 0 сверху/снизу."""
    if not traj.snapshots:
        return (0.0, 0.0)
    pnls = [_signal_payout_pct_at(traj.entry_yes, yes, traj.side) for _, yes in traj.snapshots]
    return min(min(pnls), 0.0), max(max(pnls), 0.0)


def terminal_pnl_pct(*, entry_yes: float, side: str, outcome: str) -> float:
    """Payoff на резолве в pnl%: WIN → cost идёт в 1, LOSS → в 0 (−100%)."""
    if outcome == "LOSS":
        return -100.0
    terminal_yes = 1.0 if side.upper() == "YES" else 0.0
    return _signal_payout_pct_at(entry_yes, terminal_yes, side)


@dataclass
class ExitResult:
    exit_reason: str  # 'stop_loss' | 'take_profit' | 'resolution'
    exit_pnl_gross_pct: float
    net_pnl_pct: float
    exit_at: datetime | None  # None для resolution


def simulate_tp_sl(
    traj: SignalTrajectory,
    *,
    sl_pct: float | None,
    tp_pct: float | None,
    haircut_pct: float,
) -> ExitResult:
    """Прогон TP/SL по пути. Early-exit = 2 ноги (полный haircut), hold = 1 нога (haircut/2).

    Проверка порогов на каждом снапшоте по фактической цене (как simulate_signal
    в shadow_simulation): exit по цене снапшота, не по порогу — консервативно
    учитывает gap через порог.
    """
    for captured_at, yes in traj.snapshots:
        pnl = _signal_payout_pct_at(traj.entry_yes, yes, traj.side)
        if tp_pct is not None and pnl >= tp_pct:
            return ExitResult("take_profit", pnl, pnl - haircut_pct, captured_at)
        if sl_pct is not None and pnl <= sl_pct:
            return ExitResult("stop_loss", pnl, pnl - haircut_pct, captured_at)
    terminal = terminal_pnl_pct(entry_yes=traj.entry_yes, side=traj.side, outcome=traj.outcome)
    return ExitResult("resolution", terminal, terminal - haircut_pct / 2.0, None)


def bootstrap_mean_ci(
    values: list[float],
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float, float]:
    """Percentile-bootstrap 95% CI среднего. Пустой вход → (nan, nan)."""
    if not values:
        return (float("nan"), float("nan"))
    arr = np.asarray(values, dtype=float)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(arr), size=(resamples, len(arr)), dtype=np.int32)
    means = arr[idx].mean(axis=1)
    return (float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5)))


def staleness_stats(traj: SignalTrajectory) -> dict[str, float]:
    """Метрики свежести пути: доля повторов цены между соседними снапшотами,
    максимальное плато (подряд одинаковых цен), медианный интервал в минутах."""
    prices = [yes for _, yes in traj.snapshots]
    times = [t for t, _ in traj.snapshots]
    repeats = sum(1 for a, b in zip(prices, prices[1:]) if a == b)
    pairs = max(len(prices) - 1, 1)
    max_plateau, cur = 1, 1
    for a, b in zip(prices, prices[1:]):
        cur = cur + 1 if a == b else 1
        max_plateau = max(max_plateau, cur)
    intervals = [(t2 - t1).total_seconds() / 60.0 for t1, t2 in zip(times, times[1:])]
    return {
        "repeat_share": repeats / pairs,
        "max_plateau": max_plateau,
        "median_interval_min": float(np.median(intervals)) if intervals else float("nan"),
    }


COHORT_SQL = """
SELECT ds.id::text AS signal_id, ds.detector, ds.side,
       ds.current_price::float8 AS entry_yes, ds.outcome, ds.outcome_proxy,
       ds.generated_at, ds.resolved_at,
       array_agg(s.captured_at ORDER BY s.captured_at) AS captured_ats,
       array_agg(s.yes_price::float8 ORDER BY s.captured_at) AS yes_prices
  FROM detector_signals ds
  JOIN detector_signal_price_snapshots s ON s.detector_signal_id = ds.id
 WHERE ds.resolved = true
   AND ds.outcome IN ('WIN', 'LOSS')
   AND ds.source = 'live_v2'
 GROUP BY ds.id
HAVING count(*) >= $1
"""

# Цензура rolling-окна: сравнить горизонты когорты со всеми сигналами
CENSORING_SQL = """
SELECT detector,
       count(*) FILTER (WHERE resolved) AS n_resolved,
       count(*) FILTER (WHERE NOT resolved) AS n_open,
       count(*) FILTER (WHERE resolved AND outcome = 'EXPIRED') AS n_expired,
       count(*) FILTER (WHERE resolved AND outcome_proxy) AS n_proxy,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY horizon_sec)
           FILTER (WHERE resolved) AS median_horizon_resolved,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY horizon_sec)
           FILTER (WHERE NOT resolved) AS median_horizon_open
  FROM detector_signals
 WHERE source = 'live_v2' AND generated_at > now() - interval '30 days'
 GROUP BY detector ORDER BY n_resolved DESC
"""


COHORT_QUERY_TIMEOUT_S = 300.0  # Seq Scan + JOIN на 25M снапшотов не укладывается в
# дефолтный command_timeout пула 30s (тот же урок, что vacuum-markets, см.
# app/jobs/vacuum_maintenance.py) — обходим fetch() явным acquire+timeout.


async def load_cohort(min_snapshots: int = MIN_SNAPSHOTS, limit: int | None = None) -> list[SignalTrajectory]:
    from app.services.database import get_pool

    sql = COHORT_SQL + (f" LIMIT {int(limit)}" if limit else "")
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, min_snapshots, timeout=COHORT_QUERY_TIMEOUT_S)
    return [
        SignalTrajectory(
            signal_id=r["signal_id"],
            detector=r["detector"],
            side=r["side"],
            entry_yes=float(r["entry_yes"]),
            outcome=r["outcome"],
            generated_at=r["generated_at"],
            resolved_at=r["resolved_at"],
            snapshots=list(zip(r["captured_ats"], r["yes_prices"])),
        )
        for r in rows
        if 0.0 < float(r["entry_yes"]) < 1.0
    ]


def analyze_mae_mfe(trajs: list[SignalTrajectory]) -> list[str]:
    """Секция 1 спеки: распределения MAE/MFE WIN vs LOSS + P(WIN | MAE <= -x)."""
    lines = ["# MAE/MFE — evidence", ""]
    by_detector: dict[str, list[SignalTrajectory]] = defaultdict(list)
    for t in trajs:
        by_detector[t.detector].append(t)
    for detector, group in sorted(by_detector.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"## {detector} (n={len(group)})")
        if len(group) < MIN_N_PER_DETECTOR:
            lines.append("**insufficient n (<40) — без вердикта**\n")
            continue
        wins = [t for t in group if t.outcome == "WIN"]
        losses = [t for t in group if t.outcome == "LOSS"]
        for label, sub in (("WIN", wins), ("LOSS", losses)):
            maes = [compute_mae_mfe(t)[0] for t in sub]
            mfes = [compute_mae_mfe(t)[1] for t in sub]
            if not maes:
                lines.append(f"- {label}: n=0")
                continue
            lines.append(
                f"- {label}: n={len(sub)}, MAE p50={np.median(maes):.1f}% "
                f"p10={np.percentile(maes, 10):.1f}%, "
                f"MFE p50={np.median(mfes):.1f}% p90={np.percentile(mfes, 90):.1f}%"
            )
        lines.append("")
        lines.append("| MAE ≤ −x | n | P(WIN) | Wilson 95% CI |")
        lines.append("|---|---|---|---|")
        pairs = [(compute_mae_mfe(t)[0], t.outcome) for t in group]
        for x in MAE_THRESHOLDS:
            hit = [(mae, oc) for mae, oc in pairs if mae <= -x]
            n_hit = len(hit)
            n_win = sum(1 for _, oc in hit if oc == "WIN")
            if n_hit == 0:
                lines.append(f"| −{x:.0f}% | 0 | — | — |")
                continue
            lo, hi = wilson_score_interval(n_win, n_hit)
            lines.append(f"| −{x:.0f}% | {n_hit} | {n_win / n_hit:.2f} | [{lo:.2f}, {hi:.2f}] |")
        lines.append("")
    return lines


def analyze_grid(trajs: list[SignalTrajectory]) -> list[str]:
    """Секция 2 спеки: SL × TP грид vs hold-to-resolution, per-detector, per-haircut."""
    lines = [
        "# Counterfactual TP/SL grid — evidence",
        "",
        "net pnl% = mean по сигналам; CI = percentile bootstrap 95%.",
        "hold-to-resolution = ячейка (SL=None, TP=None).",
        "",
    ]
    by_detector: dict[str, list[SignalTrajectory]] = defaultdict(list)
    for t in trajs:
        by_detector[t.detector].append(t)
    for detector, group in sorted(by_detector.items(), key=lambda kv: -len(kv[1])):
        if len(group) < MIN_N_PER_DETECTOR:
            continue
        lines.append(f"## {detector} (n={len(group)})")
        for haircut in HAIRCUT_PCTS:
            hold = [simulate_tp_sl(t, sl_pct=None, tp_pct=None, haircut_pct=haircut).net_pnl_pct for t in group]
            hold_lo, hold_hi = bootstrap_mean_ci(hold)
            lines.append(
                f"\n### haircut {haircut:.0f}% — hold baseline: "
                f"mean {np.mean(hold):+.1f}% CI [{hold_lo:+.1f}, {hold_hi:+.1f}]"
            )
            lines.append("")
            header = "| SL \\ TP | " + " | ".join("None" if tp is None else f"+{tp:.0f}%" for tp in TP_GRID) + " |"
            lines.append(header)
            lines.append("|" + "---|" * (len(TP_GRID) + 1))
            for sl in SL_GRID:
                cells = []
                for tp in TP_GRID:
                    pnls = [simulate_tp_sl(t, sl_pct=sl, tp_pct=tp, haircut_pct=haircut).net_pnl_pct for t in group]
                    lo, hi = bootstrap_mean_ci(pnls)
                    delta = np.mean(pnls) - np.mean(hold)
                    cells.append(f"{np.mean(pnls):+.1f} (Δ{delta:+.1f}) [{lo:+.1f},{hi:+.1f}]")
                sl_label = "None" if sl is None else f"−{abs(sl):.0f}%"
                lines.append(f"| {sl_label} | " + " | ".join(cells) + " |")
        lines.append("")
    return lines


def analyze_timing(trajs: list[SignalTrajectory]) -> list[str]:
    """Секция 3 спеки: time-to-MAE, когда loser себя выдаёт."""
    lines = [
        "# Timing — evidence",
        "",
        "time_to_mae = минуты от generated_at до снапшота с минимальным pnl%.",
        "time-to-MAE считается только по сигналам с MAE<0; колонка 'доля MAE=0' —",
        "сигналы вообще без просадки (argmin=0 у них исказил бы медиану на 0ч).",
        "",
    ]
    by_detector: dict[str, list[SignalTrajectory]] = defaultdict(list)
    for t in trajs:
        by_detector[t.detector].append(t)
    for detector, group in sorted(by_detector.items(), key=lambda kv: -len(kv[1])):
        if len(group) < MIN_N_PER_DETECTOR:
            continue
        lines.append(f"## {detector} (n={len(group)})")
        lines.append(
            "| outcome | n | доля MAE=0 | time-to-MAE p50 (ч) | p90 (ч) | "
            "доля MAE в первой четверти жизни |"
        )
        lines.append("|---|---|---|---|---|---|")
        for label in ("WIN", "LOSS"):
            sub = [t for t in group if t.outcome == label]
            if not sub:
                continue
            n_mae_zero = sum(1 for t in sub if compute_mae_mfe(t)[0] == 0.0)
            drawdown_sub = [t for t in sub if compute_mae_mfe(t)[0] < 0.0]
            ttm, early = [], 0
            for t in drawdown_sub:
                pnls = [_signal_payout_pct_at(t.entry_yes, yes, t.side) for _, yes in t.snapshots]
                i_min = int(np.argmin(pnls))
                mae_at = t.snapshots[i_min][0]
                life_sec = max((t.resolved_at - t.generated_at).total_seconds(), 1.0)
                pos = (mae_at - t.generated_at).total_seconds() / life_sec
                ttm.append((mae_at - t.generated_at).total_seconds() / 3600.0)
                early += pos <= 0.25
            p50 = f"{np.median(ttm):.1f}" if ttm else "—"
            p90 = f"{np.percentile(ttm, 90):.1f}" if ttm else "—"
            early_share = f"{early / len(drawdown_sub):.2f}" if drawdown_sub else "—"
            lines.append(
                f"| {label} | {len(sub)} | {n_mae_zero / len(sub):.2f} | {p50} | {p90} | {early_share} |"
            )
        lines.append("")
    return lines


def analyze_data_quality(trajs: list[SignalTrajectory], censoring_rows: list) -> list[str]:
    """Секция 4 спеки: staleness, цензура открытых, EXPIRED/proxy, малые n."""
    lines = [
        "# Data quality — evidence",
        "",
        "Снапшоты пишутся каждые 5 мин из markets.current_price (15-мин sync):",
        "даже на живом рынке ожидаем ~2/3 повторов подряд. repeat_share выше",
        "~0.9 на медиане = траектории почти статичны, грид на них врёт.",
        "",
    ]
    by_detector: dict[str, list[SignalTrajectory]] = defaultdict(list)
    for t in trajs:
        by_detector[t.detector].append(t)
    lines.append("| detector | n когорты | repeat_share p50 | max_plateau p50 | интервал p50 (мин) |")
    lines.append("|---|---|---|---|---|")
    for detector, group in sorted(by_detector.items(), key=lambda kv: -len(kv[1])):
        stats = [staleness_stats(t) for t in group]
        lines.append(
            f"| {detector} | {len(group)} | "
            f"{np.median([s['repeat_share'] for s in stats]):.2f} | "
            f"{np.median([s['max_plateau'] for s in stats]):.0f} | "
            f"{np.median([s['median_interval_min'] for s in stats]):.1f} |"
        )
    lines.append("")
    lines.append("## Цензура rolling-окна и состав резолвов (30 дней, все сигналы)")
    lines.append("| detector | resolved | open | EXPIRED | proxy | медиана horizon resolved (ч) | open (ч) |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in censoring_rows:
        mh_r = (r["median_horizon_resolved"] or 0) / 3600.0
        mh_o = (r["median_horizon_open"] or 0) / 3600.0
        lines.append(
            f"| {r['detector']} | {r['n_resolved']} | {r['n_open']} | {r['n_expired']} | "
            f"{r['n_proxy']} | {mh_r:.0f} | {mh_o:.0f} |"
        )
    lines.append("")
    lines.append(
        "Если медиана horizon open >> resolved — длинные горизонты недопредставлены "
        "в когорте (цензура), вердикты применимы только к коротким горизонтам."
    )
    return lines


def merge_archive(trajs: list[SignalTrajectory], archive_dir: Path) -> list[SignalTrajectory]:
    """Дописывает снапшоты из parquet-архива к траекториям когорты.

    Дедуп по (detector_signal_id, captured_at) — дубли между дневными файлами
    законны. В прогоне 2026-07-06/07 архив ещё пуст — путь для будущих прогонов.
    """
    import pyarrow.parquet as pq

    files = sorted(archive_dir.glob("snapshots_*.parquet"))
    if not files:
        return trajs
    by_id = {t.signal_id: t for t in trajs}
    for f in files:
        table = pq.read_table(f)
        for sid, ts, price in zip(
            table["detector_signal_id"].to_pylist(),
            table["captured_at"].to_pylist(),
            table["yes_price"].to_pylist(),
        ):
            traj = by_id.get(sid)
            if traj is not None:
                traj.snapshots.append((ts, float(price)))
    for t in trajs:
        seen: set = set()
        merged = []
        for ts, price in sorted(t.snapshots):
            if ts not in seen:
                seen.add(ts)
                merged.append((ts, price))
        t.snapshots = merged
    return trajs


async def main() -> None:
    parser = argparse.ArgumentParser(description="Exit-trajectories audit (Часть 1)")
    parser.add_argument("--out-dir", required=True, help="research/exit-trajectories-audit-2026-07-06")
    parser.add_argument("--min-snapshots", type=int, default=MIN_SNAPSHOTS)
    parser.add_argument("--limit", type=int, default=None, help="dry-run: ограничить когорту")
    parser.add_argument(
        "--archive-dir",
        default=None,
        help="parquet-архив траекторий (для будущих прогонов; сейчас не нужен)",
    )
    args = parser.parse_args()

    from app.services.database import close_db, fetch, init_db

    await init_db()
    try:
        trajs = await load_cohort(args.min_snapshots, args.limit)
        censoring_rows = await fetch(CENSORING_SQL)
    finally:
        await close_db()

    if args.archive_dir:
        trajs = merge_archive(trajs, Path(args.archive_dir))

    print(f"cohort: {len(trajs)} signals, detectors: {sorted({t.detector for t in trajs})}")

    out = Path(args.out_dir)
    evidence = out / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "data-quality.md").write_text("\n".join(analyze_data_quality(trajs, censoring_rows)) + "\n")
    (evidence / "mae-mfe.md").write_text("\n".join(analyze_mae_mfe(trajs)) + "\n")
    (evidence / "grid.md").write_text("\n".join(analyze_grid(trajs)) + "\n")
    (evidence / "timing.md").write_text("\n".join(analyze_timing(trajs)) + "\n")
    print(f"evidence written to {evidence}")


if __name__ == "__main__":
    asyncio.run(main())
