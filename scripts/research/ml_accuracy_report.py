#!/usr/bin/env python3
"""ml_accuracy_report — честная панель «точности» ML по 3 слоям + деньги.

«Точность» = насколько ML идеально предсказывает КУДА ставить и КОГДА. Воронка:

  Слой 1 — честность вероятности: Brier (per-event + per-row), decomposition,
           ECE, calibration slope, AUC, Brier Skill Score vs рынок, per-price-zone.
  Слой 2 — КУДА ставить (отбор): top1-per-event accuracy, precision@K с Wilson CI,
           counterfactual (отобранные EV-фильтром vs отвергнутые), lift над рынком.
  Слой 3 — КОГДА ставить (тайминг): метрики разбиты по anchor 24/72/168h +
           within-anchor бакеты по horizon_days (точнее ли у резолва).
  Деньги  — realized_roi_net (уже net после fees, хранится ×100) per-EVENT, per-category
           с защитой от концентрации: median vs avg, event-concentration,
           leave-one-event-out, side-mix, cluster bootstrap CI → флаг RELIABLE/ARTIFACT.

ЗАЩИТА ОТ АРТЕФАКТОВ («honest cohort», см. _HONEST_COHORT_CTE):
  (a) cutoff inference_at >= '2026-05-20' — исключить миграционный backfill 12-14 мая,
  (b) дедуп DISTINCT ON (condition_id, anchor_hours) последнее по inference_at,
  (c) event-health: исключить лестницы с n_yes_winners != 1 (битые метки миграции),
  (d) per-EVENT агрегация метрик (не per-row → не псевдо-N).

Источник истины исхода — markets.winner (живая), НЕ oracle_snapshots (пуста после миграции).

Запуск (внутри docker, scripts/ в образе — не bind-mount):
    docker exec -w /app oraclepoly-backend python scripts/research/ml_accuracy_report.py
    ... --out-dir research/output/ml-accuracy      # markdown в файл
    ... --json                                      # дополнительно JSON в stdout

Idempotent (read-only). Никаких правок моделей/калибраторов/порогов.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from statistics import median

# scripts/research/ → корень проекта (3 уровня вверх) для импорта app.* при запуске
# из произвольного cwd. В docker PYTHONPATH=/app тоже работает — это дополняет.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.backtest.metrics import wilson_score_interval  # noqa: E402
from app.calibration.market_picker import top1_per_event_accuracy  # noqa: E402
from app.calibration.per_event_metrics import count_events, per_event_brier  # noqa: E402
from app.calibration.validation import (  # noqa: E402
    auc_score,
    brier_decomposition,
    brier_skill_score,
    calibration_intercept_slope,
    event_cluster_bootstrap_roi,
    expected_calibration_error,
    segment_metrics,
)
from app.services.database import close_db, fetch, init_db  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

# ── Параметры honest cohort ──────────────────────────────────────────────────

# Cutoff: миграционный backfill 12-14 мая сломал метки negRisk-лестниц
# (выигравшая нога winner=NULL, проигравшие с мусорным market_yes_price=0.99) →
# инверсия калибровки. Всё до этой даты исключаем из метрик.
HONEST_CUTOFF = "2026-05-20"

# Категория получает флаг ARTIFACT если выполнено ЛЮБОЕ:
ARTIFACT_MIN_EVENTS = 8          # слишком мало событий для вывода
ARTIFACT_MAX_CONCENTRATION = 0.60  # доля топ-события в сигналах категории
ARTIFACT_LOO_SIGN_FLIP = True    # leave-one-event-out переворачивает знак ROI

PRECISION_K = 20  # топ-K сигналов по EV для precision@K
BOOTSTRAP_SEED = 42  # воспроизводимость cluster-bootstrap

# Живые детекторы (генерируют сигналы на 2026-06-02). STANDALONE имеет
# отдельный predictions-стол с anchor; прочие — только detector_signals.
LIVE_DETECTORS = (
    "STANDALONE_V2_LGB",
    "D36_obi_pressure",
    "D38_resolution_lag",
    "D3_cross_mkt_arb",
    "D26_fade_whale_extreme",
    "D44_late_convergence_carry",
)


# ── Dataclasses вывода ───────────────────────────────────────────────────────


@dataclass
class Layer1Honesty:
    """Слой 1 — честность вероятности (per anchor)."""

    anchor_hours: int
    n_rows: int
    n_events: int
    per_event_brier: float
    per_row_brier: float          # для сверки с ручным AVG(brier_squared)
    market_brier: float           # baseline = рыночная цена
    brier_skill_vs_market: float  # >0 → модель лучше рынка
    edge_vs_market: float         # market_brier − model_brier (>0 = лучше)
    ece: float
    calibration_slope: float
    calibration_intercept: float
    auc: float | None
    reliability: float            # из decomposition (low = good)
    resolution: float             # из decomposition (high = good)
    base_rate: float


@dataclass
class SegmentRow:
    segment: str
    n: int
    mean_pred: float
    mean_outcome: float
    ece: float
    auc: float | None


@dataclass
class Layer2Selection:
    """Слой 2 — куда ставить (качество отбора)."""

    anchor_hours: int
    top1_per_event_accuracy: float   # выбирает ли модель winner внутри события
    precision_at_k: float            # WR топ-K по EV
    precision_at_k_wilson_lo: float
    precision_at_k_n: int
    selected_mean_roi: float | None  # отобранные EV-фильтром (net_ev>=threshold)
    rejected_mean_roi: float | None  # отвергнутые
    selection_lift: float | None     # selected − rejected (>0 = фильтр полезен)
    market_lift_brier: float         # насколько модель бьёт «ставить как рынок»


@dataclass
class Layer3TimingBucket:
    """Слой 3 — когда (within-anchor по horizon_days)."""

    anchor_hours: int
    horizon_bucket: str
    n: int
    per_row_brier: float
    base_rate: float


@dataclass
class MoneyCategory:
    """Деньги по категории с защитой от концентрации."""

    category: str
    detector: str
    n_signals: int
    n_events: int
    avg_roi_net_pct: float       # уже net после fees, в %
    median_roi_net_pct: float
    win_rate: float
    event_concentration: float   # доля топ-события в сигналах
    loo_avg_roi_pct: float | None  # leave-one-event-out: убрать крупнейшее
    side_no_share: float         # доля side=NO (перекос)
    bootstrap_roi_lo: float
    bootstrap_roi_hi: float
    reliability_flag: str        # RELIABLE / ARTIFACT / WEAK
    artifact_reasons: list[str] = field(default_factory=list)


@dataclass
class AccuracyReport:
    generated_at: str
    cohort_cutoff: str
    layer1: list[Layer1Honesty] = field(default_factory=list)
    segments: dict[int, list[SegmentRow]] = field(default_factory=dict)
    layer2: list[Layer2Selection] = field(default_factory=list)
    layer3: list[Layer3TimingBucket] = field(default_factory=list)
    money: list[MoneyCategory] = field(default_factory=list)


# ── SQL ──────────────────────────────────────────────────────────────────────

# Honest cohort для STANDALONE: дедуп + cutoff + event-health (через markets.winner).
# Возвращает по одной строке на (condition_id, anchor_hours) — последнюю по inference_at.
_STANDALONE_COHORT_SQL = f"""
WITH dedup AS (
    SELECT DISTINCT ON (sv.condition_id, sv.anchor_hours)
        sv.condition_id, sv.anchor_hours, sv.event_id, sv.category,
        sv.predicted_p_yes, sv.market_yes_price, sv.ev_yes_pct, sv.ev_no_pct,
        sv.horizon_days
    FROM standalone_v2_predictions sv
    WHERE sv.inference_at >= '{HONEST_CUTOFF}'
    ORDER BY sv.condition_id, sv.anchor_hours, sv.inference_at DESC
),
event_health AS (
    -- событие здорово если ровно 1 YES-победитель ИЛИ одиночный рынок (не лестница)
    SELECT d.event_id
    FROM dedup d JOIN markets m ON m.condition_id = d.condition_id
    WHERE m.winner IS NOT NULL
    GROUP BY d.event_id
    HAVING COUNT(*) FILTER (WHERE LOWER(m.winner) = 'yes') = 1
        OR COUNT(DISTINCT d.condition_id) = 1
)
SELECT
    d.condition_id, d.anchor_hours,
    COALESCE(d.event_id, d.condition_id) AS event_id,
    d.category, d.predicted_p_yes, d.market_yes_price,
    d.ev_yes_pct, d.ev_no_pct, d.horizon_days,
    CASE WHEN LOWER(m.winner) = 'yes' THEN 1 ELSE 0 END AS yes_won
FROM dedup d
JOIN markets m ON m.condition_id = d.condition_id
JOIN event_health eh ON eh.event_id = d.event_id
WHERE m.winner IS NOT NULL AND LOWER(m.winner) IN ('yes', 'no')
"""

# Деньги: realized_roi_net по детектору с категорией через markets.
# detector_signals.market_id == markets.condition_id (подтверждено фазой 0).
_MONEY_SQL = """
SELECT
    ds.detector,
    COALESCE(m.category, '(unknown)') AS category,
    COALESCE(ds.event_id, ds.market_id) AS event_id,
    ds.side,
    ds.realized_roi_net,
    ds.outcome
FROM detector_signals ds
LEFT JOIN markets m ON m.condition_id = ds.market_id
WHERE ds.detector = ANY($1::text[])
  AND ds.source = 'live_v2'
  AND ds.resolved = true
  AND ds.realized_roi_net IS NOT NULL
"""


# ── Слой 1: честность вероятности ────────────────────────────────────────────


def _compute_layer1(rows: list) -> tuple[list[Layer1Honesty], dict[int, list[SegmentRow]]]:
    by_anchor: dict[int, list] = {}
    for r in rows:
        by_anchor.setdefault(int(r["anchor_hours"]), []).append(r)

    out: list[Layer1Honesty] = []
    seg_out: dict[int, list[SegmentRow]] = {}
    for anchor, ar in sorted(by_anchor.items()):
        event_ids = [r["event_id"] for r in ar]
        p = [float(r["predicted_p_yes"]) for r in ar]
        mkt = [float(r["market_yes_price"]) for r in ar]
        y = [int(r["yes_won"]) for r in ar]

        decomp = brier_decomposition(y, p)
        per_row_b = decomp.brier_score
        market_b = sum((mp - yy) ** 2 for mp, yy in zip(mkt, y)) / len(y)
        intercept, slope = calibration_intercept_slope(y, p)
        base = sum(y) / len(y)

        out.append(
            Layer1Honesty(
                anchor_hours=anchor,
                n_rows=len(ar),
                n_events=count_events(event_ids),
                per_event_brier=round(per_event_brier(event_ids, p, y), 4),
                per_row_brier=round(per_row_b, 4),
                market_brier=round(market_b, 4),
                brier_skill_vs_market=round(brier_skill_score(y, p, mkt), 4),
                edge_vs_market=round(market_b - per_row_b, 4),
                ece=round(expected_calibration_error(y, p), 4),
                calibration_slope=round(slope, 3),
                calibration_intercept=round(intercept, 3),
                auc=round(auc_score(y, p), 3) if auc_score(y, p) is not None else None,
                reliability=round(decomp.reliability, 4),
                resolution=round(decomp.resolution, 4),
                base_rate=round(base, 3),
            )
        )
        seg_out[anchor] = [
            SegmentRow(
                segment=s.segment, n=s.n,
                mean_pred=round(s.mean_pred, 3), mean_outcome=round(s.mean_outcome, 3),
                ece=round(s.ece, 4),
                auc=round(s.auc, 3) if s.auc is not None else None,
            )
            for s in segment_metrics(y, p, p)  # segment по предсказанной вероятности
        ]
    return out, seg_out


# ── Слой 2: куда ставить (отбор) ─────────────────────────────────────────────


def _compute_layer2(rows: list, money_by_cond: dict) -> list[Layer2Selection]:
    """money_by_cond: condition_id → realized_roi_net (для counterfactual)."""
    by_anchor: dict[int, list] = {}
    for r in rows:
        by_anchor.setdefault(int(r["anchor_hours"]), []).append(r)

    out: list[Layer2Selection] = []
    for anchor, ar in sorted(by_anchor.items()):
        event_ids = [r["event_id"] for r in ar]
        p = [float(r["predicted_p_yes"]) for r in ar]
        mkt = [float(r["market_yes_price"]) for r in ar]
        y = [int(r["yes_won"]) for r in ar]

        # precision@K: топ-K по max(ev_yes, ev_no) — где модель «уверена в EV»
        scored = sorted(
            ar,
            key=lambda r: max(float(r["ev_yes_pct"] or 0), float(r["ev_no_pct"] or 0)),
            reverse=True,
        )
        topk = scored[:PRECISION_K]
        topk_wins = sum(int(r["yes_won"]) for r in topk)
        wl, _wh = wilson_score_interval(topk_wins, len(topk)) if topk else (0.0, 0.0)

        # counterfactual: отобранные EV-фильтром (max EV > 0) vs отвергнутые,
        # по реальному realized_roi_net (через condition_id).
        sel_roi, rej_roi = [], []
        for r in ar:
            roi = money_by_cond.get(r["condition_id"])
            if roi is None:
                continue
            max_ev = max(float(r["ev_yes_pct"] or 0), float(r["ev_no_pct"] or 0))
            (sel_roi if max_ev > 0 else rej_roi).append(roi)
        sel_mean = round(sum(sel_roi) / len(sel_roi), 2) if sel_roi else None
        rej_mean = round(sum(rej_roi) / len(rej_roi), 2) if rej_roi else None
        lift = round(sel_mean - rej_mean, 2) if (sel_mean is not None and rej_mean is not None) else None

        out.append(
            Layer2Selection(
                anchor_hours=anchor,
                top1_per_event_accuracy=round(top1_per_event_accuracy(event_ids, p, y), 3),
                precision_at_k=round(topk_wins / len(topk), 3) if topk else 0.0,
                precision_at_k_wilson_lo=round(wl, 3),
                precision_at_k_n=len(topk),
                selected_mean_roi=sel_mean,
                rejected_mean_roi=rej_mean,
                selection_lift=lift,
                market_lift_brier=round(brier_skill_score(y, p, mkt), 4),
            )
        )
    return out


# ── Слой 3: когда (timing по horizon) ────────────────────────────────────────

_HORIZON_BUCKETS = [(0, 1, "<1d"), (1, 3, "1-3d"), (3, 7, "3-7d"), (7, 1e9, ">7d")]


def _compute_layer3(rows: list) -> list[Layer3TimingBucket]:
    out: list[Layer3TimingBucket] = []
    by_anchor: dict[int, list] = {}
    for r in rows:
        by_anchor.setdefault(int(r["anchor_hours"]), []).append(r)

    for anchor, ar in sorted(by_anchor.items()):
        for lo, hi, label in _HORIZON_BUCKETS:
            bucket = [r for r in ar if r["horizon_days"] is not None and lo <= float(r["horizon_days"]) < hi]
            if not bucket:
                continue
            p = [float(r["predicted_p_yes"]) for r in bucket]
            y = [int(r["yes_won"]) for r in bucket]
            brier = sum((pp - yy) ** 2 for pp, yy in zip(p, y)) / len(y)
            out.append(
                Layer3TimingBucket(
                    anchor_hours=anchor,
                    horizon_bucket=label,
                    n=len(bucket),
                    per_row_brier=round(brier, 4),
                    base_rate=round(sum(y) / len(y), 3),
                )
            )
    return out


# ── Деньги: per-category с защитой от концентрации ───────────────────────────


def _compute_money(rows: list) -> list[MoneyCategory]:
    # группируем по (detector, category)
    groups: dict[tuple[str, str], list] = {}
    for r in rows:
        groups.setdefault((r["detector"], r["category"]), []).append(r)

    out: list[MoneyCategory] = []
    for (detector, category), gr in groups.items():
        rois = [float(r["realized_roi_net"]) for r in gr]
        events = [r["event_id"] for r in gr]
        n_events = len(set(events))
        avg = sum(rois) / len(rois)

        # event-concentration: доля сигналов в крупнейшем событии
        ev_counts: dict[str, int] = {}
        for e in events:
            ev_counts[e] = ev_counts.get(e, 0) + 1
        top_event = max(ev_counts, key=ev_counts.get)
        concentration = ev_counts[top_event] / len(gr)

        # leave-one-event-out: убрать крупнейшее событие, пересчитать avg
        loo_rois = [float(r["realized_roi_net"]) for r in gr if r["event_id"] != top_event]
        loo_avg = round(sum(loo_rois) / len(loo_rois), 2) if loo_rois else None

        # side-mix
        no_share = sum(1 for r in gr if r["side"] == "NO") / len(gr)

        # bootstrap CI (cluster по событию)
        boot_lo, boot_hi = event_cluster_bootstrap_roi(events, rois, seed=BOOTSTRAP_SEED)

        wins = sum(1 for r in gr if r["outcome"] == "WIN")
        resolved_traded = sum(1 for r in gr if r["outcome"] in ("WIN", "LOSS"))
        wr = wins / resolved_traded if resolved_traded else 0.0

        # классификация
        reasons: list[str] = []
        if n_events < ARTIFACT_MIN_EVENTS:
            reasons.append(f"мало событий (n={n_events}<{ARTIFACT_MIN_EVENTS})")
        if concentration >= ARTIFACT_MAX_CONCENTRATION:
            reasons.append(f"концентрация {concentration:.0%} на 1 событии")
        if loo_avg is not None and (loo_avg > 0) != (avg > 0):
            reasons.append(f"LOO переворачивает знак ({avg:+.1f}→{loo_avg:+.1f})")
        if no_share >= 0.95 or no_share <= 0.05:
            reasons.append(f"side-перекос {no_share:.0%} NO")

        if reasons and (n_events < ARTIFACT_MIN_EVENTS or concentration >= ARTIFACT_MAX_CONCENTRATION
                        or (loo_avg is not None and (loo_avg > 0) != (avg > 0))):
            flag = "ARTIFACT"
        elif reasons:
            flag = "WEAK"
        else:
            flag = "RELIABLE"

        out.append(
            MoneyCategory(
                category=category, detector=detector,
                n_signals=len(gr), n_events=n_events,
                avg_roi_net_pct=round(avg, 2),
                median_roi_net_pct=round(median(rois), 2),
                win_rate=round(wr, 3),
                event_concentration=round(concentration, 3),
                loo_avg_roi_pct=loo_avg,
                side_no_share=round(no_share, 3),
                bootstrap_roi_lo=round(boot_lo, 2),
                bootstrap_roi_hi=round(boot_hi, 2),
                reliability_flag=flag,
                artifact_reasons=reasons,
            )
        )
    # сортировка: RELIABLE сверху, потом по avg roi
    order = {"RELIABLE": 0, "WEAK": 1, "ARTIFACT": 2}
    out.sort(key=lambda m: (order[m.reliability_flag], -m.avg_roi_net_pct))
    return out


# ── Markdown ─────────────────────────────────────────────────────────────────


def render_markdown(rep: AccuracyReport) -> str:
    ln: list[str] = []
    ln.append("# ML Accuracy — панель точности (куда · когда · деньги)")
    ln.append("")
    ln.append(f"> Generated: {rep.generated_at}")
    ln.append(f"> Honest cohort cutoff: `inference_at >= {rep.cohort_cutoff}` "
              "(дедуп по condition_id+anchor, event-health фильтр)")
    ln.append("")

    ln.append("## Слой 1 — честность вероятности (STANDALONE, per anchor)")
    ln.append("")
    ln.append("| anchor | n_ev | per-event Brier | per-row Brier | market Brier | edge | BSS | ECE | slope | AUC | base |")
    ln.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rep.layer1:
        auc = f"{r.auc:.3f}" if r.auc is not None else "n/a"
        ln.append(f"| {r.anchor_hours}h | {r.n_events} | {r.per_event_brier:.4f} | "
                  f"{r.per_row_brier:.4f} | {r.market_brier:.4f} | **{r.edge_vs_market:+.4f}** | "
                  f"{r.brier_skill_vs_market:+.3f} | {r.ece:.4f} | {r.calibration_slope:.2f} | {auc} | {r.base_rate:.2f} |")
    ln.append("")
    ln.append("_edge = market_brier − model_brier (>0 → модель бьёт рынок). "
              "slope≈1 = калибровано. reliability/resolution в JSON._")
    ln.append("")

    ln.append("## Слой 2 — куда ставить (качество отбора)")
    ln.append("")
    ln.append("| anchor | top1/event | precision@K | Wilson lo | selected ROI | rejected ROI | lift | market-BSS |")
    ln.append("|---|---|---|---|---|---|---|---|")
    for r in rep.layer2:
        sel = f"{r.selected_mean_roi:+.1f}%" if r.selected_mean_roi is not None else "n/a"
        rej = f"{r.rejected_mean_roi:+.1f}%" if r.rejected_mean_roi is not None else "n/a"
        lift = f"**{r.selection_lift:+.1f}**" if r.selection_lift is not None else "n/a"
        ln.append(f"| {r.anchor_hours}h | {r.top1_per_event_accuracy:.3f} | "
                  f"{r.precision_at_k:.3f} (n={r.precision_at_k_n}) | {r.precision_at_k_wilson_lo:.3f} | "
                  f"{sel} | {rej} | {lift} | {r.market_lift_brier:+.3f} |")
    ln.append("")
    ln.append("_top1/event = выбирает ли модель winner внутри события. "
              "lift = ROI(отобранные EV-фильтром) − ROI(отвергнутые), >0 → фильтр полезен._")
    ln.append("")

    ln.append("## Слой 3 — когда ставить (тайминг по horizon)")
    ln.append("")
    ln.append("| anchor | horizon | n | per-row Brier | base |")
    ln.append("|---|---|---|---|---|")
    for r in rep.layer3:
        ln.append(f"| {r.anchor_hours}h | {r.horizon_bucket} | {r.n} | {r.per_row_brier:.4f} | {r.base_rate:.2f} |")
    ln.append("")

    ln.append("## Деньги — realized ROI net по категориям (после fees)")
    ln.append("")
    ln.append("| flag | detector | category | n_sig | n_ev | avg ROI | median | WR | conc | LOO | NO% | boot CI |")
    ln.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for m in rep.money:
        loo = f"{m.loo_avg_roi_pct:+.1f}" if m.loo_avg_roi_pct is not None else "—"
        emoji = {"RELIABLE": "✅", "WEAK": "⚠️", "ARTIFACT": "❌"}[m.reliability_flag]
        ln.append(f"| {emoji} {m.reliability_flag} | {m.detector} | {m.category} | {m.n_signals} | "
                  f"{m.n_events} | {m.avg_roi_net_pct:+.1f}% | {m.median_roi_net_pct:+.1f}% | {m.win_rate:.2f} | "
                  f"{m.event_concentration:.0%} | {loo} | {m.side_no_share:.0%} | "
                  f"[{m.bootstrap_roi_lo:+.1f},{m.bootstrap_roi_hi:+.1f}] |")
    ln.append("")
    ln.append("_avg ROI = net после fees, в %. conc = доля топ-события. "
              "LOO = avg без крупнейшего события. boot CI = 95% cluster-bootstrap по событиям._")
    ln.append("")
    ln.append("### Артефакты (не доверять)")
    for m in rep.money:
        if m.reliability_flag == "ARTIFACT":
            ln.append(f"- **{m.detector}/{m.category}** ({m.avg_roi_net_pct:+.1f}%): {'; '.join(m.artifact_reasons)}")
    ln.append("")
    return "\n".join(ln)


# ── Main ─────────────────────────────────────────────────────────────────────


async def build_report() -> AccuracyReport:
    standalone_rows = await fetch(_STANDALONE_COHORT_SQL)
    money_rows = await fetch(_MONEY_SQL, list(LIVE_DETECTORS))
    logger.info("loaded standalone_cohort=%d money_rows=%d", len(standalone_rows), len(money_rows))

    # condition_id → realized_roi_net (для counterfactual слоя 2)
    money_by_cond: dict[str, float] = {}
    for r in money_rows:
        if r["detector"] == "STANDALONE_V2_LGB":
            money_by_cond[r["event_id"]] = float(r["realized_roi_net"])

    layer1, segments = _compute_layer1(standalone_rows)
    layer2 = _compute_layer2(standalone_rows, money_by_cond)
    layer3 = _compute_layer3(standalone_rows)
    money = _compute_money(money_rows)

    return AccuracyReport(
        generated_at=datetime.now(UTC).isoformat(),
        cohort_cutoff=HONEST_CUTOFF,
        layer1=layer1, segments=segments,
        layer2=layer2, layer3=layer3, money=money,
    )


async def run(out_dir: str | None, emit_json: bool) -> None:
    await init_db()
    try:
        rep = await build_report()
    finally:
        await close_db()

    md = render_markdown(rep)
    if out_dir:
        ts = rep.generated_at.replace(":", "").replace("-", "")[:15]
        out_path = Path(out_dir) / f"{ts}.md"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(md)
        logger.info("report written: %s", out_path)
    else:
        print(md)

    if emit_json:
        print("\n=== JSON ===")
        print(json.dumps(_report_to_jsonable(rep), ensure_ascii=False, indent=2))


def _report_to_jsonable(rep: AccuracyReport) -> dict:
    d = asdict(rep)
    # segments dict[int] → str keys для JSON
    d["segments"] = {str(k): v for k, v in d["segments"].items()}
    return d


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", help="Директория для markdown-отчёта (иначе stdout).")
    ap.add_argument("--json", action="store_true", help="Дополнительно вывести JSON в stdout.")
    args = ap.parse_args()
    asyncio.run(run(args.out_dir, args.json))


if __name__ == "__main__":
    main()
