"""G18 shrinkage vs raw WR — offline walk-forward replay (audit task 1.2).

bin_lookup (app/services/g18_bin_calibration.py) возвращает сырой wr_observed
бина при n_observations >= MIN_OBS_FOR_LOOKUP (30). При n=30 Wilson CI ±17pp —
шум бина напрямую шумит Kelly-стейк через EV sizer.

Гипотеза: shrinkage к кумулятивному detector×category prior даёт лучший Brier,
чем сырой wr_observed:

    wr_shrunk = (n * wr_bin + k * wr_prior) / (n + k)

prior = кумулятивный WR на уровне (detector, category) до недели t (шире бина,
меньше шума). k — сила прайора (30 или 50 наблюдений эквивалента).
Альтернатива для сравнения — Wilson score midpoint бина (center без margin).

Walk-forward, по неделям:
    для недели t бины строятся ТОЛЬКО из сигналов ДО начала недели t (кумулятивно,
    расширяющееся окно) — leak-free. Оцениваются на сигналах недели t, чей бин
    имеет n >= MIN_OBS_FOR_LOOKUP в train-срезе. Копим Brier по 4 вариантам:
    raw / shrunk(k=30) / shrunk(k=50) / wilson_midpoint.

Биннинг идентичен app/services/g18_bin_calibration.py::_price_bin (floor(price*10)/10)
и app/jobs/calibration_bins_refit.py (та же price_bin формула, тот же source column
ds.current_price, тот же category join на markets.category с fallback 'unknown').

Прод-код НЕ используется и не трогается — это read-only офлайн-реплей поверх тех
же SQL-паттернов, чтобы не размагничивать boundary с live bin_lookup().

Usage (на проде, ssh <your-server>, docker exec):
    docker exec oraclepoly-backend python scripts/research/g18_shrinkage_replay.py \
        --detectors D14,D36,D38 --start 2025-01-01 --end 2026-07-01 \
        --out /tmp/g18_shrinkage_replay.md

    (или локально в venv на проде: .venv/bin/python3 scripts/research/g18_shrinkage_replay.py ...)

⚠️ Локально НЕ запускать — локальная БД пуста, только прод через SSH (см. CLAUDE.md).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

# Биннинг идентичен app/services/g18_bin_calibration.py и app/jobs/calibration_bins_refit.py.
N_BINS = 10
MIN_OBS_FOR_LOOKUP = 30  # см. app/services/g18_bin_calibration.py::MIN_OBS_FOR_LOOKUP

# Тяжёлый JOIN detector_signals x markets на всей истории — дефолтный
# command_timeout пула (30s) может не хватить (тот же урок, что exit_trajectories_audit.py
# / vacuum-markets). Обходим fetch() явным acquire+timeout.
QUERY_TIMEOUT_S = 300.0


# ---------------------------------------------------------------------------
# Pure math — no I/O. Тестируется локально без БД (tests/research/test_g18_shrinkage_replay.py).
# ---------------------------------------------------------------------------


def price_bin(price: float) -> float:
    """Map 0..1 price to 0.0, 0.1, ..., 0.9 bin floor. Идентично _price_bin в g18_bin_calibration.py."""
    if not 0.0 <= price <= 1.0:
        raise ValueError(f"price must be in [0, 1], got {price}")
    bucket = int(price * N_BINS)
    if bucket >= N_BINS:  # price == 1.0
        bucket = N_BINS - 1
    return round(bucket / N_BINS, 1)


def shrink_wr(n: int, wr_bin: float, wr_prior: float, k: int) -> float:
    """Shrinkage estimator: wr_shrunk = (n*wr_bin + k*wr_prior) / (n + k).

    k — сила прайора в эквиваленте наблюдений. Больше k → сильнее тянет к prior.
    n=0 → возвращает wr_prior (нет бинового сигнала вообще).
    """
    if n < 0:
        raise ValueError(f"n must be >= 0, got {n}")
    if k < 0:
        raise ValueError(f"k must be >= 0, got {k}")
    if n == 0 and k == 0:
        raise ValueError("n and k cannot both be 0 (undefined)")
    return (n * wr_bin + k * wr_prior) / (n + k)


def wilson_midpoint(wins: int, n: int, z: float = 1.96) -> float:
    """Wilson score interval center (не lower bound) — точечная оценка вместо wr_observed.

    center = (p_hat + z^2/(2n)) / (1 + z^2/n). При n→inf сходится к p_hat, при
    малых n стягивается к 0.5 (регуляризация к неинформативному прайору).
    """
    if n < 0:
        raise ValueError(f"n must be >= 0, got {n}")
    if wins < 0 or wins > n:
        raise ValueError(f"wins ({wins}) must be in [0, n={n}]")
    if n == 0:
        return 0.5
    p_hat = wins / n
    denom = 1.0 + z * z / n
    center = (p_hat + z * z / (2.0 * n)) / denom
    return float(min(1.0, max(0.0, center)))


def brier_update(acc_sq_error: float, acc_n: int, pred: float, outcome: int) -> tuple[float, int]:
    """Аккумулятор суммы квадратичных ошибок Brier + счётчик. outcome — 1/0 (WIN/LOSS)."""
    if outcome not in (0, 1):
        raise ValueError(f"outcome must be 0 or 1, got {outcome}")
    if not 0.0 <= pred <= 1.0:
        raise ValueError(f"pred must be in [0, 1], got {pred}")
    return acc_sq_error + (pred - outcome) ** 2, acc_n + 1


def brier_mean(acc_sq_error: float, acc_n: int) -> float | None:
    """Средний Brier из аккумулятора. None если нет наблюдений (избегаем div/0)."""
    if acc_n == 0:
        return None
    return acc_sq_error / acc_n


@dataclass
class WeekMetrics:
    """Per-week aggregation для одного detector×category (или detector-level rollup)."""

    n_signals: int = 0
    acc: dict[str, tuple[float, int]] = field(
        default_factory=lambda: {"raw": (0.0, 0), "shrunk30": (0.0, 0), "shrunk50": (0.0, 0), "wilson": (0.0, 0)}
    )

    def add(self, variant: str, pred: float, outcome: int) -> None:
        sq, n = self.acc[variant]
        self.acc[variant] = brier_update(sq, n, pred, outcome)

    def brier(self, variant: str) -> float | None:
        sq, n = self.acc[variant]
        return brier_mean(sq, n)


def week_start(dt: datetime) -> datetime:
    """ISO week start (Monday 00:00 UTC) для группировки walk-forward окон."""
    from datetime import timedelta

    d = dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
    d0 = d.replace(hour=0, minute=0, second=0, microsecond=0)
    return d0 - timedelta(days=d0.weekday())


def verdict_shrunk_vs_raw(
    brier_raw_total: float,
    brier_variant_total: float,
    weekly_deltas: list[float],
) -> str:
    """Вердикт по критерию задачи: shrunk лучше raw если

    1) суммарный Brier ниже (variant < raw), И
    2) не хуже в >=60% недель (delta = variant_week - raw_week <= 0 в >=60% недель).

    weekly_deltas: список (brier_variant_week - brier_raw_week) по неделям, где
    оба варианта имели >=1 наблюдение.
    """
    if not weekly_deltas:
        return "NO_DATA"
    better_total = brier_variant_total < brier_raw_total
    n_not_worse = sum(1 for d in weekly_deltas if d <= 0)
    frac_not_worse = n_not_worse / len(weekly_deltas)
    if better_total and frac_not_worse >= 0.60:
        return "SHRUNK_BETTER"
    return "RAW_BETTER_OR_MIXED"


# ---------------------------------------------------------------------------
# I/O — DB access. НЕ вызывать локально (пустая БД). Только на проде.
# ---------------------------------------------------------------------------

_SIGNALS_SQL_TEMPLATE = """
SELECT
    ds.detector,
    COALESCE(m.category, 'unknown') AS category,
    ds.current_price::float8 AS current_price,
    ds.generated_at,
    ds.outcome
  FROM detector_signals ds
  LEFT JOIN markets m ON m.polymarket_id = ds.market_id
 WHERE ds.resolved = true
   AND ds.outcome IN ('WIN', 'LOSS')
   AND ds.current_price IS NOT NULL
   AND ds.current_price > 0
   AND ds.current_price < 1
   AND ds.generated_at >= $1
   AND ds.generated_at < $2
   AND ({detector_filter})
 ORDER BY ds.generated_at ASC
"""


def _build_query(start: datetime, end: datetime, detector_codes: list[str]) -> tuple[str, list]:
    """Строит SQL + params: detector matches любой из коротких кодов (D14 -> D14 или D14_%)."""
    params: list = [start, end]
    clauses = []
    idx = 3
    for code in detector_codes:
        clauses.append(f"(ds.detector = ${idx} OR ds.detector LIKE ${idx + 1})")
        params.append(code)
        params.append(f"{code}\\_%")
        idx += 2
    where_detectors = " OR ".join(clauses)
    sql = _SIGNALS_SQL_TEMPLATE.format(detector_filter=where_detectors)
    return sql, params


async def load_signals(start: datetime, end: datetime, detector_codes: list[str]) -> list[dict]:
    """Load resolved signals за окно, отфильтрованные по коротким кодам детекторов."""
    from app.services.database import get_pool

    sql, params = _build_query(start, end, detector_codes)
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params, timeout=QUERY_TIMEOUT_S)
    return [
        {
            "detector": r["detector"],
            "category": r["category"],
            "current_price": float(r["current_price"]),
            "generated_at": r["generated_at"],
            "won": 1 if r["outcome"] == "WIN" else 0,
        }
        for r in rows
    ]


def run_walk_forward(signals: list[dict]) -> dict:
    """Walk-forward по неделям: train = кумулятивная история ДО недели t, eval = неделя t.

    Возвращает per-detector агрегаты: недель, сигналов, Brier per variant, per-week
    deltas (для критерия >=60% недель не хуже).
    """
    if not signals:
        return {}

    for s in signals:
        s["week"] = week_start(s["generated_at"])
        s["bin"] = price_bin(s["current_price"])

    weeks = sorted({s["week"] for s in signals})
    by_detector: dict[str, dict] = {}

    for wk in weeks:
        train = [s for s in signals if s["week"] < wk]
        eval_week = [s for s in signals if s["week"] == wk]
        if not train or not eval_week:
            continue

        # Bin stats (detector, category, bin) на train
        bin_stats: dict[tuple[str, str, float], tuple[int, int]] = {}
        for s in train:
            key = (s["detector"], s["category"], s["bin"])
            n, w = bin_stats.get(key, (0, 0))
            bin_stats[key] = (n + 1, w + s["won"])

        # Prior: (detector, category) кумулятивный WR на train
        prior_stats: dict[tuple[str, str], tuple[int, int]] = {}
        for s in train:
            key = (s["detector"], s["category"])
            n, w = prior_stats.get(key, (0, 0))
            prior_stats[key] = (n + 1, w + s["won"])

        week_by_det: dict[str, WeekMetrics] = {}
        for s in eval_week:
            key = (s["detector"], s["category"], s["bin"])
            n, w = bin_stats.get(key, (0, 0))
            if n < MIN_OBS_FOR_LOOKUP:
                continue  # тот же гейт что bin_lookup() в проде — сигнал не калибруется
            wr_bin = w / n

            prior_key = (s["detector"], s["category"])
            pn, pw = prior_stats.get(prior_key, (0, 0))
            wr_prior = pw / pn if pn > 0 else 0.5  # неинформативный prior если детектор новый

            pred_raw = wr_bin
            pred_shrunk30 = shrink_wr(n, wr_bin, wr_prior, k=30)
            pred_shrunk50 = shrink_wr(n, wr_bin, wr_prior, k=50)
            pred_wilson = wilson_midpoint(w, n)

            det = s["detector"]
            wm = week_by_det.setdefault(det, WeekMetrics())
            wm.n_signals += 1
            wm.add("raw", pred_raw, s["won"])
            wm.add("shrunk30", pred_shrunk30, s["won"])
            wm.add("shrunk50", pred_shrunk50, s["won"])
            wm.add("wilson", pred_wilson, s["won"])

        for det, wm in week_by_det.items():
            agg = by_detector.setdefault(
                det,
                {
                    "n_weeks": 0,
                    "n_signals": 0,
                    "acc": {"raw": (0.0, 0), "shrunk30": (0.0, 0), "shrunk50": (0.0, 0), "wilson": (0.0, 0)},
                    "weekly_deltas": {"shrunk30": [], "shrunk50": [], "wilson": []},
                },
            )
            agg["n_weeks"] += 1
            agg["n_signals"] += wm.n_signals
            for variant in ("raw", "shrunk30", "shrunk50", "wilson"):
                sq, n = agg["acc"][variant]
                wsq, wn = wm.acc[variant]
                agg["acc"][variant] = (sq + wsq, n + wn)

            raw_week = wm.brier("raw")
            if raw_week is not None:
                for variant in ("shrunk30", "shrunk50", "wilson"):
                    variant_week = wm.brier(variant)
                    if variant_week is not None:
                        agg["weekly_deltas"][variant].append(variant_week - raw_week)

    return by_detector


def render_report(by_detector: dict, args: argparse.Namespace) -> str:
    lines = [
        "---",
        "spec: g18_shrinkage_replay",
        "audit_task: 1.2",
        f"generated_at: {datetime.now(UTC).isoformat()}",
        f"window: {args.start} -> {args.end}",
        f"detectors: {args.detectors}",
        "---",
        "",
        "# G18 Shrinkage vs Raw WR — Walk-Forward Replay",
        "",
        f"**Window:** {args.start} → {args.end}",
        f"**Detectors:** {args.detectors}",
        f"**MIN_OBS_FOR_LOOKUP:** {MIN_OBS_FOR_LOOKUP} (совпадает с прод bin_lookup)",
        "",
        "## Per detector",
        "",
        "| Detector | Weeks | Signals | Brier raw | Brier shrunk(k=30) | Δ30 | "
        "Brier shrunk(k=50) | Δ50 | Brier wilson | ΔW | Verdict (shrunk30) |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]

    for det in sorted(by_detector.keys()):
        agg = by_detector[det]
        brier_raw = brier_mean(*agg["acc"]["raw"])
        brier_s30 = brier_mean(*agg["acc"]["shrunk30"])
        brier_s50 = brier_mean(*agg["acc"]["shrunk50"])
        brier_w = brier_mean(*agg["acc"]["wilson"])

        if brier_raw is None:
            continue

        d30 = brier_s30 - brier_raw if brier_s30 is not None else None
        d50 = brier_s50 - brier_raw if brier_s50 is not None else None
        dw = brier_w - brier_raw if brier_w is not None else None

        verdict = verdict_shrunk_vs_raw(brier_raw, brier_s30, agg["weekly_deltas"]["shrunk30"]) if brier_s30 is not None else "NO_DATA"

        def fmt(v: float | None) -> str:
            return f"{v:.4f}" if v is not None else "n/a"

        def fmt_delta(v: float | None) -> str:
            return f"{v:+.4f}" if v is not None else "n/a"

        lines.append(
            f"| {det} | {agg['n_weeks']} | {agg['n_signals']} | {fmt(brier_raw)} | "
            f"{fmt(brier_s30)} | {fmt_delta(d30)} | {fmt(brier_s50)} | {fmt_delta(d50)} | "
            f"{fmt(brier_w)} | {fmt_delta(dw)} | {verdict} |"
        )

    lines += [
        "",
        "## Критерий вердикта",
        "",
        "`SHRUNK_BETTER` если суммарный Brier ниже raw И не хуже в >=60% недель "
        "(delta_week <= 0 в >=60% пересекающихся недель). Иначе `RAW_BETTER_OR_MIXED`.",
        "",
        "Сигналы недели t предсказываются только если их bin имеет n >= "
        f"{MIN_OBS_FOR_LOOKUP} наблюдений в кумулятивном train (все недели < t) — "
        "тот же гейт, что и live bin_lookup(). Bin-стата и prior строятся из train "
        "среза, без заглядывания в будущее.",
    ]
    return "\n".join(lines) + "\n"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--detectors", default="D14,D36,D38", help="Comma-separated короткие коды детекторов")
    parser.add_argument("--start", default="2025-01-01")
    parser.add_argument("--end", default="2026-07-01")
    parser.add_argument("--out", default="/tmp/g18_shrinkage_replay.md")
    args = parser.parse_args()

    detector_codes = [d.strip() for d in args.detectors.split(",") if d.strip()]
    start = datetime.fromisoformat(args.start).replace(tzinfo=UTC)
    end = datetime.fromisoformat(args.end).replace(tzinfo=UTC)

    from app.services.database import close_db, init_db

    await init_db()
    try:
        print(f"Loading signals {start} -> {end} for detectors={detector_codes}")
        signals = await load_signals(start, end, detector_codes)
    finally:
        await close_db()

    if not signals:
        print("ERROR: no resolved signals in window for given detectors")
        return 1
    print(f"Loaded {len(signals):,} resolved signals")

    by_detector = run_walk_forward(signals)
    if not by_detector:
        print("ERROR: no weeks with n >= MIN_OBS_FOR_LOOKUP in train — nothing to evaluate")
        return 1

    report = render_report(by_detector, args)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report)
    print(f"\nReport: {out_path}")
    for det, agg in sorted(by_detector.items()):
        raw = brier_mean(*agg["acc"]["raw"])
        s30 = brier_mean(*agg["acc"]["shrunk30"])
        print(f"  {det}: weeks={agg['n_weeks']} signals={agg['n_signals']} brier_raw={raw} brier_shrunk30={s30}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
