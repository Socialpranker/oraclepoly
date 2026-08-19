#!/usr/bin/env python3
"""Signal decay study — пост-сигнальная траектория цены → optimal hold-period.

Зачем: у нас есть `detector_signal_price_snapshots` (плотные снимки yes_price
каждые 5 мин для каждого сигнала, ~5-10ч покрытия). Это единственный источник
данных о том, что цена делает *после* сигнала — в canonical такого нет (там
только цена на 8 фиксированных anchor'ах до резолва, а не эволюция после входа).

Из этих траекторий выводим per-detector:
  1. Decay-кривую: средний PnL% по бинам времени-от-сигнала (форма движения).
  2. Time-to-peak: где в среднем находится максимум PnL (оптимальный hold).
  3. Optimal TP/SL: grid-перебор порогов, симуляция first-touch exit, поиск
     комбинации с максимальным net PnL → сравнение с текущими overrides.

Экономика PnL переиспользует `_signal_payout_pct_at` из shadow_simulation, чтобы
рекомендации были 1:1 совместимы с тем, как virtual exec реально считает P&L
(cost-basis: YES cost=entry_yes, NO cost=1-entry_yes; см. shadow_simulation.py).

First-touch семантика grid-симуляции совпадает с position_manager: идём по
траектории, первый снимок где pnl>=tp → take_profit, где pnl<=sl → stop_loss,
иначе hold-to-resolution через realized_roi_net. Fee = ROUND_TRIP_FEE_PCT (4%).

ВАЖНО — ограничения данных (см. memory live_signal_trajectories_2026-05-28):
  - Снимки пишутся только с ~2026-05-12. Детекторы с backfill-сигналами или
    почти без живых сигналов в окне имеют 0 траекторий → не анализируемы здесь.
  - Достаточно данных (>40 сигналов) только у STANDALONE_V2_LGB, D38, D43, D36.
    D44/D37 погранично (~47-49). Остальные пропускаем.
  - HOLD_TO_RESOLUTION детекторы (D38): TP/SL grid неинформативен (edge только
    at resolution), но decay-кривая всё равно показательна.

Usage (на VPS, внутри backend-контейнера — там есть app.services.database):
    docker exec -i oraclepoly-backend python -m scripts.research.signal_decay_study \\
        --detector STANDALONE_V2_LGB --out research/signal-decay-2026-05/standalone_v2.md
    # без --detector прогоняет все детекторы с достаточным N и пишет сводку:
    docker exec -i oraclepoly-backend python -m scripts.research.signal_decay_study \\
        --out-dir research/signal-decay-2026-05/
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.services.database import close_db, fetch, init_db  # noqa: E402
from app.services.shadow_simulation import (  # noqa: E402
    ROUND_TRIP_FEE_PCT,
    _signal_payout_pct_at,
    _tp_sl_for,
)

# ────────────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────────────

# Минимум resolved сигналов с траекторией для валидного вывода. Wilson lower
# bound на binary outcomes валиден при N>=40 (memory wilson_min_sample_size).
MIN_SIGNALS_FOR_VERDICT = 40
# Погранично: считаем, но помечаем как low-confidence.
MIN_SIGNALS_BORDERLINE = 25

# Бины времени-от-сигнала (минуты). Последний бин — "хвост".
TIME_BINS_MIN: list[tuple[str, float, float]] = [
    ("0-30m", 0.0, 30.0),
    ("30-60m", 30.0, 60.0),
    ("1-2h", 60.0, 120.0),
    ("2-4h", 120.0, 240.0),
    ("4-8h", 240.0, 480.0),
    ("8h+", 480.0, float("inf")),
]

# Grid для TP/SL оптимизации (в % cost-basis PnL).
TP_GRID: list[float] = [10.0, 15.0, 20.0, 25.0, 30.0, 40.0, 50.0]
SL_GRID: list[float] = [-10.0, -15.0, -20.0, -25.0, -30.0, -40.0]

# Зона торгуемости. Цена YES вне [floor, ceiling] = рынок фактически резолвнулся
# (нет ликвидности продать на самом краю). Cost-basis PnL на yes=0.0005 даёт
# математические +566%, но это unrealizable — мы бы держали до резолва и получили
# realized_roi_net (с реальным haircut). Поэтому для exit-достижимости клипаем
# цену к границе зоны: снимок с yes=0.0005 трактуется как exit по yes=floor.
# Это убирает выбросы +500% из decay-кривой и time-to-peak (они были артефактом
# близких-к-резолву снимков, а не реальной торгуемой возможностью).
TRADEABLE_FLOOR: float = 0.02
TRADEABLE_CEILING: float = 0.98

# Кандидаты на timeout (часы максимального hold перед принудительным exit по
# текущей цене). None = держать до резолва.
TIMEOUT_GRID_H: list[float | None] = [1.0, 2.0, 4.0, 8.0, None]


# ────────────────────────────────────────────────────────────────────────────
# Data loading
# ────────────────────────────────────────────────────────────────────────────

_DETECTORS_WITH_DATA_SQL = """
SELECT
    ds.detector,
    COUNT(DISTINCT ds.id) FILTER (WHERE ds.resolved)             AS resolved,
    COUNT(DISTINCT s.detector_signal_id)                         AS with_traj
FROM detector_signals ds
JOIN detector_signal_price_snapshots s ON s.detector_signal_id = ds.id
WHERE ds.resolved = true
GROUP BY ds.detector
HAVING COUNT(DISTINCT s.detector_signal_id) >= $1
ORDER BY COUNT(DISTINCT s.detector_signal_id) DESC
"""

# Одним запросом: все resolved сигналы детектора + их траектории. Сортировка по
# (signal, captured_at) даёт упорядоченный price path на питон-стороне без N+1.
_SIGNALS_WITH_PATH_SQL = """
SELECT
    ds.id::text                                  AS signal_id,
    ds.side                                       AS side,
    ds.current_price::float8                      AS entry_yes,
    ds.outcome                                    AS outcome,
    ds.realized_roi_net::float8                   AS realized_roi_net,
    ds.generated_at                               AS generated_at,
    s.captured_at                                 AS captured_at,
    s.yes_price::float8                           AS yes_price,
    EXTRACT(EPOCH FROM (s.captured_at - ds.generated_at)) / 60.0 AS minutes_since
FROM detector_signals ds
JOIN detector_signal_price_snapshots s ON s.detector_signal_id = ds.id
WHERE ds.detector = $1
  AND ds.resolved = true
  AND ds.current_price > 0
  AND ds.current_price < 1
ORDER BY ds.id, s.captured_at
"""


@dataclass
class SignalPath:
    """Одна resolved-сигнальная траектория."""

    signal_id: str
    side: str
    entry_yes: float
    outcome: str | None
    realized_roi_net: float | None
    # Параллельные массивы, отсортированы по времени.
    minutes: list[float]
    yes_prices: list[float]

    def pnl_series(self) -> list[float]:
        """Realizable PnL% на каждом снимке (cost-basis, как в shadow_simulation).

        Цена клипается к торгуемой зоне [TRADEABLE_FLOOR, TRADEABLE_CEILING] —
        exit на самом краю (yes≈0 или ≈1) unrealizable, см. константы выше.
        """
        return [
            _signal_payout_pct_at(
                self.entry_yes,
                min(TRADEABLE_CEILING, max(TRADEABLE_FLOOR, yp)),
                self.side,
            )
            for yp in self.yes_prices
        ]


async def load_signal_paths(detector: str) -> list[SignalPath]:
    """Загрузить все resolved-траектории детектора одним запросом."""
    rows = await fetch(_SIGNALS_WITH_PATH_SQL, detector)
    paths: dict[str, SignalPath] = {}
    for r in rows:
        sid = r["signal_id"]
        p = paths.get(sid)
        if p is None:
            p = SignalPath(
                signal_id=sid,
                side=r["side"],
                entry_yes=float(r["entry_yes"]),
                outcome=r["outcome"],
                realized_roi_net=(
                    float(r["realized_roi_net"])
                    if r["realized_roi_net"] is not None
                    else None
                ),
                minutes=[],
                yes_prices=[],
            )
            paths[sid] = p
        p.minutes.append(float(r["minutes_since"]))
        p.yes_prices.append(float(r["yes_price"]))
    # Отбрасываем сигналы где минуты невалидны (снимок раньше сигнала — clock skew).
    return [p for p in paths.values() if p.minutes and p.minutes[-1] >= 0]


# ────────────────────────────────────────────────────────────────────────────
# Decay curve
# ────────────────────────────────────────────────────────────────────────────


def decay_curve(paths: list[SignalPath]) -> list[dict]:
    """Средний PnL% по бинам времени-от-сигнала (форма движения цены).

    Для каждого бина усредняем PnL по всем снимкам всех сигналов, попавшим в окно.
    Это "усреднённая траектория" — куда в среднем идёт цена после входа.
    """
    bin_pnls: dict[str, list[float]] = {name: [] for name, _, _ in TIME_BINS_MIN}
    for p in paths:
        pnls = p.pnl_series()
        for m, pnl in zip(p.minutes, pnls):
            for name, lo, hi in TIME_BINS_MIN:
                if lo <= m < hi:
                    bin_pnls[name].append(pnl)
                    break
    out = []
    for name, lo, _hi in TIME_BINS_MIN:
        vals = bin_pnls[name]
        out.append(
            {
                "bin": name,
                "n_snaps": len(vals),
                "mean_pnl": round(float(np.mean(vals)), 2) if vals else None,
                "median_pnl": round(float(np.median(vals)), 2) if vals else None,
                "p25": round(float(np.percentile(vals, 25)), 2) if vals else None,
                "p75": round(float(np.percentile(vals, 75)), 2) if vals else None,
            }
        )
    return out


def time_to_peak(paths: list[SignalPath]) -> dict:
    """Распределение времени до максимального PnL по каждому сигналу.

    Показывает, как долго в среднем стоит держать, чтобы поймать пик движения.
    """
    peak_minutes: list[float] = []
    peak_pnls: list[float] = []
    for p in paths:
        pnls = p.pnl_series()
        if not pnls:
            continue
        idx = int(np.argmax(pnls))
        peak_minutes.append(p.minutes[idx])
        peak_pnls.append(pnls[idx])
    if not peak_minutes:
        return {}
    arr = np.array(peak_minutes)
    return {
        "n": len(peak_minutes),
        "median_min": round(float(np.median(arr)), 1),
        "p25_min": round(float(np.percentile(arr, 25)), 1),
        "p75_min": round(float(np.percentile(arr, 75)), 1),
        "mean_peak_pnl": round(float(np.mean(peak_pnls)), 2),
    }


# ────────────────────────────────────────────────────────────────────────────
# TP/SL grid simulation
# ────────────────────────────────────────────────────────────────────────────


def _simulate_one(
    path: SignalPath,
    tp_pct: float,
    sl_pct: float,
    timeout_h: float | None,
) -> float:
    """Net PnL% одного сигнала под (tp, sl, timeout). First-touch семантика.

    Совпадает с shadow_simulation.simulate_signal: идём по снимкам по порядку,
    первый раз pnl>=tp → выходим с (tp - fee), pnl<=sl → (sl - fee). Если за
    timeout_h ничего не сработало — выходим по текущей цене на момент timeout.
    Если timeout=None и нет trigger — hold-to-resolution через realized_roi_net.
    """
    pnls = path.pnl_series()
    timeout_min = timeout_h * 60.0 if timeout_h is not None else float("inf")
    last_pnl_before_timeout = 0.0
    for m, pnl in zip(path.minutes, pnls):
        if m > timeout_min:
            # Вышли по таймауту — фиксируем последнюю цену до него.
            return last_pnl_before_timeout - ROUND_TRIP_FEE_PCT
        last_pnl_before_timeout = pnl
        if pnl >= tp_pct:
            return tp_pct - ROUND_TRIP_FEE_PCT
        if pnl <= sl_pct:
            return sl_pct - ROUND_TRIP_FEE_PCT
    # Нет trigger в пределах траектории.
    if timeout_h is not None:
        # Таймаут за пределами покрытия снимков → выход по последнему снимку.
        return last_pnl_before_timeout - ROUND_TRIP_FEE_PCT
    # Hold-to-resolution: realized_roi_net уже net (включает fees+slippage) И уже
    # в процентах (NOT доля). См. memory g3_tail_risk_units_bug_2026-05-28 +
    # эмпирика: NO@0.85 WIN → realized_roi_net=82.8 (= +82.8%, gross был +566%).
    # НЕ умножать на 100 (это была причина абсурдных +421%/сигнал в первом прогоне).
    if path.realized_roi_net is not None:
        return path.realized_roi_net
    if path.outcome == "EXPIRED":
        return 0.0
    return last_pnl_before_timeout - ROUND_TRIP_FEE_PCT


def grid_search(paths: list[SignalPath]) -> list[dict]:
    """Перебор TP × SL × timeout. Для каждой комбинации — агрегаты PnL.

    Возвращает отсортированный по mean_net_pnl список (лучшие сверху).
    """
    results = []
    n = len(paths)
    for tp in TP_GRID:
        for sl in SL_GRID:
            for timeout in TIMEOUT_GRID_H:
                pnls = [_simulate_one(p, tp, sl, timeout) for p in paths]
                arr = np.array(pnls)
                wins = int((arr > 0).sum())
                # Sortino-знаменатель: downside deviation (только отрицательные).
                downside = arr[arr < 0]
                dd = float(np.std(downside)) if len(downside) > 0 else 0.0
                mean = float(np.mean(arr))
                results.append(
                    {
                        "tp": tp,
                        "sl": sl,
                        "timeout_h": timeout,
                        "n": n,
                        "mean_net_pnl": round(mean, 2),
                        "total_net_pnl": round(float(arr.sum()), 1),
                        "win_rate": round(100.0 * wins / n, 1) if n else 0.0,
                        "downside_dev": round(dd, 2),
                        # Sortino-подобный: mean / downside_dev (memory: Sharpe N/A).
                        "sortino": round(mean / dd, 3) if dd > 0 else None,
                    }
                )
    results.sort(key=lambda r: r["mean_net_pnl"], reverse=True)
    return results


def current_baseline(detector: str, paths: list[SignalPath]) -> dict:
    """PnL под текущими _DETECTOR_OVERRIDES (timeout=None — как сейчас в проде)."""
    tp, sl = _tp_sl_for(detector)
    pnls = [_simulate_one(p, tp, sl, None) for p in paths]
    arr = np.array(pnls)
    n = len(paths)
    wins = int((arr > 0).sum())
    return {
        "tp": tp,
        "sl": sl,
        "timeout_h": None,
        "n": n,
        "mean_net_pnl": round(float(np.mean(arr)), 2),
        "total_net_pnl": round(float(arr.sum()), 1),
        "win_rate": round(100.0 * wins / n, 1) if n else 0.0,
    }


# ────────────────────────────────────────────────────────────────────────────
# Report rendering
# ────────────────────────────────────────────────────────────────────────────


def render_report(detector: str, paths: list[SignalPath]) -> tuple[str, dict]:
    """Markdown-отчёт по детектору + machine-readable summary dict."""
    import pandas as pd  # локально — heavy import только при рендере

    n = len(paths)
    confidence = (
        "OK"
        if n >= MIN_SIGNALS_FOR_VERDICT
        else ("LOW" if n >= MIN_SIGNALS_BORDERLINE else "INSUFFICIENT")
    )

    curve = decay_curve(paths)
    ttp = time_to_peak(paths)
    baseline = current_baseline(detector, paths)
    grid = grid_search(paths)
    best = grid[0] if grid else None

    lines: list[str] = []
    lines.append(f"# Signal Decay Study — {detector}")
    lines.append("")
    lines.append(f"**Дата:** {pd.Timestamp.now().date()}")
    lines.append(f"**Resolved сигналов с траекторией:** {n} (confidence: {confidence})")
    lines.append("**Источник:** `detector_signal_price_snapshots` (5-мин снимки)")
    lines.append(f"**Fee model:** {ROUND_TRIP_FEE_PCT}% round-trip (как в shadow_simulation)")
    lines.append("")

    if confidence == "INSUFFICIENT":
        lines.append(f"> ⚠️ N={n} < {MIN_SIGNALS_BORDERLINE}. Выводы статистически невалидны.")
        lines.append("")

    # Decay curve. Главная метрика — MEDIAN (mean искажается тяжёлым правым
    # хвостом NO-сигналов с дешёвым входом; см. TRADEABLE_* и комментарий ниже).
    lines.append("## Decay-кривая (PnL% по времени-от-сигнала)")
    lines.append("")
    lines.append("> Читать по **Median** — mean искажают NO-сигналы с дешёвым входом "
                 "(payout 5-6×), у них один выигрыш = сотни % cost-basis.")
    lines.append("")
    lines.append("| Бин | Снимков | **Median PnL%** | p25 | p75 | Mean (справочно) |")
    lines.append("|---|---|---|---|---|---|")
    for c in curve:
        lines.append(
            f"| {c['bin']} | {c['n_snaps']} | **{c['median_pnl']}** | "
            f"{c['p25']} | {c['p75']} | {c['mean_pnl']} |"
        )
    lines.append("")

    # Time to peak
    if ttp:
        lines.append("## Time-to-peak (когда PnL максимален)")
        lines.append("")
        lines.append(f"- **Медиана:** {ttp['median_min']} мин (p25={ttp['p25_min']}, p75={ttp['p75_min']})")
        lines.append(f"- **Средний пиковый PnL:** {ttp['mean_peak_pnl']}%")
        lines.append("")

    # Current baseline vs best
    lines.append("## Текущие параметры vs найденный оптимум")
    lines.append("")
    lines.append("| | TP% | SL% | Timeout | Mean net PnL% | Win rate | Total net PnL% |")
    lines.append("|---|---|---|---|---|---|---|")
    lines.append(
        f"| **Сейчас (prod)** | {baseline['tp']} | {baseline['sl']} | hold | "
        f"{baseline['mean_net_pnl']} | {baseline['win_rate']}% | {baseline['total_net_pnl']} |"
    )
    if best:
        to = "hold" if best["timeout_h"] is None else f"{best['timeout_h']}h"
        lines.append(
            f"| **Оптимум (grid)** | {best['tp']} | {best['sl']} | {to} | "
            f"{best['mean_net_pnl']} | {best['win_rate']}% | {best['total_net_pnl']} |"
        )
    lines.append("")

    if best:
        delta = best["mean_net_pnl"] - baseline["mean_net_pnl"]
        lines.append(f"**Δ mean net PnL: {delta:+.2f} пп** на сигнал "
                     f"(× {n} сигналов = {delta * n:+.0f} пп суммарно).")
        lines.append("")

    # Top-10 grid
    lines.append("## Топ-10 комбинаций TP/SL/timeout (по mean net PnL)")
    lines.append("")
    lines.append("| TP% | SL% | Timeout | Mean net PnL% | Win rate | Sortino | Total |")
    lines.append("|---|---|---|---|---|---|---|")
    for g in grid[:10]:
        to = "hold" if g["timeout_h"] is None else f"{g['timeout_h']}h"
        lines.append(
            f"| {g['tp']} | {g['sl']} | {to} | {g['mean_net_pnl']} | "
            f"{g['win_rate']}% | {g['sortino']} | {g['total_net_pnl']} |"
        )
    lines.append("")

    summary = {
        "detector": detector,
        "n_signals": n,
        "confidence": confidence,
        "time_to_peak_median_min": ttp.get("median_min") if ttp else None,
        "baseline": baseline,
        "best": best,
        "delta_mean_pnl": round(best["mean_net_pnl"] - baseline["mean_net_pnl"], 2)
        if best
        else None,
    }
    return "\n".join(lines), summary


# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────


async def run(detector: str | None, out: str | None, out_dir: str | None) -> None:
    await init_db()
    try:
        if detector:
            detectors = [detector]
        else:
            rows = await fetch(_DETECTORS_WITH_DATA_SQL, MIN_SIGNALS_BORDERLINE)
            detectors = [r["detector"] for r in rows]
            print(f"Детекторы с >={MIN_SIGNALS_BORDERLINE} траекториями: {detectors}")

        summaries = []
        for det in detectors:
            print(f"\n=== {det} ===")
            paths = await load_signal_paths(det)
            print(f"  загружено {len(paths)} resolved-траекторий")
            if not paths:
                print("  пропуск (нет данных)")
                continue
            report, summary = render_report(det, paths)
            summaries.append(summary)

            # Куда писать отчёт.
            if out and detector:
                out_path = Path(out)
            elif out_dir:
                out_path = Path(out_dir) / f"{det}.md"
            else:
                out_path = None

            if out_path:
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(report)
                print(f"  отчёт: {out_path}")
            else:
                print(report)

        # Сводка по всем.
        if len(summaries) > 1:
            print("\n" + "=" * 60)
            print("СВОДКА")
            print("=" * 60)
            for s in summaries:
                b = s["best"]
                if b:
                    to = "hold" if b["timeout_h"] is None else f"{b['timeout_h']}h"
                    print(
                        f"{s['detector']:32} N={s['n_signals']:4} "
                        f"({s['confidence']:5}) peak@{s['time_to_peak_median_min']}m  "
                        f"opt: TP{b['tp']}/SL{b['sl']}/{to}  "
                        f"Δ{s['delta_mean_pnl']:+.1f}pp/sig"
                    )
    finally:
        await close_db()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--detector", help="Один детектор. Без него — все с достаточным N.")
    ap.add_argument("--out", help="Путь к отчёту (только с --detector).")
    ap.add_argument("--out-dir", help="Директория для отчётов всех детекторов.")
    args = ap.parse_args()
    asyncio.run(run(args.detector, args.out, args.out_dir))


if __name__ == "__main__":
    main()
