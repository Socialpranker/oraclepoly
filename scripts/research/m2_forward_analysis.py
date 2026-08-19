"""M2 forward-движение анализ — ГЛАВНАЯ МЕТРИКА M2_liquidity_vacuum.

Истинная метрика M2 = forward-Δ YES-цены на T+30/60м от current_price(t0),
НЕ winner hold-to-resolution. Источник: detector_signal_price_snapshots.

Запускать ТОЛЬКО на проде (read-only, без side-effects):
    docker exec oraclepoly-backend python scripts/research/m2_forward_analysis.py [--md]

Вердикт по kill-критериям §7 спека:
  PURSUE  ⇐ Wilson lower на whale_in_window=false (T+30 И T+60) > 0.50 при N≥40
  KILL    ⇐ CI пересекает 0.50 / всё на whale=true / N<40 за 6 недель
Спек: docs/superpowers/specs/2026-06-21-m2-liquidity-vacuum-design.md §5,§7
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.backtest.metrics import wilson_score_interval
from app.services.database import close_db, fetch, init_db

DETECTOR = "M2_liquidity_vacuum"
EPSILON = 0.002  # |delta| < 0.002 (полтика) → «без движения», исключаем из Wilson
WILSON_N_MIN = 40  # порог достоверности (decisions/wilson_min_sample_size.md)
TOLERANCE_SEC = 5 * 60  # ±5м (cron-сетка 5м)

_SIGNALS_SQL = """
SELECT id, side, current_price, generated_at, evidence
  FROM detector_signals
 WHERE detector = $1
"""

_SNAPS_SQL = """
SELECT detector_signal_id, captured_at, yes_price
  FROM detector_signal_price_snapshots
 WHERE detector_signal_id = ANY($1::uuid[])
 ORDER BY captured_at
"""


def _nearest_snapshot(snaps: list[dict], t0, offset_sec: int) -> float | None:
    """Ближайший yes_price к T0+offset в окне ±TOLERANCE_SEC, иначе None."""
    target = t0.timestamp() + offset_sec
    best, best_dist = None, None
    for s in snaps:
        dist = abs(s["captured_at"].timestamp() - target)
        if dist <= TOLERANCE_SEC and (best_dist is None or dist < best_dist):
            best, best_dist = float(s["yes_price"]), dist
    return best


def _success(side: str, delta: float) -> bool | None:
    """Движение в предсказанную сторону. None = |delta|<EPSILON (без движения)."""
    if abs(delta) < EPSILON:
        return None
    if side == "YES":
        return delta > 0
    return delta < 0  # NO: ждём падение YES


def _wilson_block(label: str, successes: int, total: int) -> str:
    lo, hi = wilson_score_interval(successes, total, 0.95) if total else (0.0, 0.0)
    rate = (successes / total) if total else 0.0
    return f"  {label:<28} N={total:<4} success={rate:.3f} Wilson[{lo:.3f}, {hi:.3f}]"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--md", action="store_true", help="дамп .md в research/")
    args = parser.parse_args()

    await init_db()
    try:
        signals = await fetch(_SIGNALS_SQL, DETECTOR)
        if not signals:
            print("M2: нет сигналов — нечего анализировать.")
            return
        ids = [s["id"] for s in signals]
        snap_rows = await fetch(_SNAPS_SQL, ids)
        by_signal: dict = {}
        for r in snap_rows:
            by_signal.setdefault(r["detector_signal_id"], []).append(r)

        # Сбор success per (horizon, разрез).
        # buckets[(horizon, cut)] = [bool, ...]; deltas[(horizon, cut)] = [float, ...]
        buckets: dict = {}
        deltas: dict = {}
        cover = {30: 0, 60: 0}

        for sig in signals:
            snaps = by_signal.get(sig["id"], [])
            evid = sig["evidence"] or {}
            whale = bool(evid.get("whale_in_window"))
            depleted = evid.get("depleted_side")
            side = sig["side"]
            p0 = float(sig["current_price"])
            for horizon, off in ((30, 1800), (60, 3600)):
                fwd = _nearest_snapshot(snaps, sig["generated_at"], off)
                if fwd is None:
                    continue
                cover[horizon] += 1
                delta = fwd - p0
                ok = _success(side, delta)
                if ok is None:
                    continue
                for cut in ("all", f"side:{depleted}", f"whale:{whale}"):
                    buckets.setdefault((horizon, cut), []).append(ok)
                    deltas.setdefault((horizon, cut), []).append(delta)

        # Отчёт.
        lines = [f"# M2 forward-анализ — {len(signals)} сигналов\n"]
        lines.append(f"Coverage: T+30 {cover[30]}/{len(signals)}, T+60 {cover[60]}/{len(signals)}\n")
        for horizon in (30, 60):
            lines.append(f"\n## T+{horizon}м")
            for cut in ("all", "side:ask", "side:bid", "whale:False", "whale:True"):
                vals = buckets.get((horizon, cut), [])
                lines.append(_wilson_block(cut, sum(vals), len(vals)))
                ds = deltas.get((horizon, cut), [])
                if ds:
                    lines.append(f"      |delta| avg={sum(abs(d) for d in ds) / len(ds):.4f} median={median(ds):+.4f}")

        # Вердикт: главный разрез = whale:False, оба горизонта.
        verdict = _verdict(buckets)
        lines.append(f"\n## ВЕРДИКТ: {verdict}\n")

        out = "\n".join(lines)
        print(out)
        if args.md:
            path = Path("research") / "m2-forward-analysis.md"
            path.write_text(out)
            print(f"\n→ {path}")
    finally:
        await close_db()


def _verdict(buckets: dict) -> str:
    key30 = buckets.get((30, "whale:False"), [])
    key60 = buckets.get((60, "whale:False"), [])
    if len(key30) < WILSON_N_MIN or len(key60) < WILSON_N_MIN:
        return f"NEED-MORE-DATA (vac-без-whale N={len(key30)}/{len(key60)} < {WILSON_N_MIN})"
    lo30 = wilson_score_interval(sum(key30), len(key30), 0.95)[0]
    lo60 = wilson_score_interval(sum(key60), len(key60), 0.95)[0]
    if lo30 > 0.50 and lo60 > 0.50:
        return f"PURSUE (vac-без-whale Wilson lower T+30={lo30:.3f} T+60={lo60:.3f} > 0.50)"
    return f"KILL (vac-без-whale Wilson lower T+30={lo30:.3f} T+60={lo60:.3f} ≤ 0.50)"


if __name__ == "__main__":
    asyncio.run(main())
