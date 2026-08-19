"""ML accuracy snapshot — периодический снимок панели «точности» в БД.

Cron 'ml-accuracy-snapshot' (weekly). Вызывает движок ml_accuracy_report.build_report
(honest cohort, 3 слоя + деньги), flatten'ит в long-формат и пишет в
ml_accuracy_snapshots. Дашборд читает последний снимок + строит тренд.

Read-only по отношению к моделям. Единственная запись — снимок метрик.
См. scripts/research/ml_accuracy_report.py + migrations/..._ml_accuracy_snapshots.sql.
"""

from __future__ import annotations

import json

import structlog

from app.services.database import executemany
from scripts.research.ml_accuracy_report import build_report

logger = structlog.get_logger()


_INSERT_SQL = """
INSERT INTO ml_accuracy_snapshots
    (snapshot_at, layer, detector, scope, metric_key, metric_value,
     cohort_n, n_events, reliability, meta)
VALUES (now(), $1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
"""


def _flatten(rep) -> list[tuple]:
    """AccuracyReport → строки для ml_accuracy_snapshots (long-формат).

    Каждая строка: (layer, detector, scope, metric_key, value, cohort_n, n_events,
    reliability, meta_json). detector='STANDALONE_V2_LGB' для слоёв 1-3 (только
    standalone имеет anchor+вероятности); деньги — per (detector, category).
    """
    rows: list[tuple] = []
    STAND = "STANDALONE_V2_LGB"

    # Слой 1 — честность вероятности (per anchor)
    for r in rep.layer1:
        scope = f"{r.anchor_hours}h"
        seg_meta = json.dumps(
            {"segments": [s for s in (rep.segments.get(r.anchor_hours) or [])]},
            ensure_ascii=False,
            default=lambda o: o.__dict__,
        )
        for key in (
            "per_event_brier",
            "per_row_brier",
            "market_brier",
            "brier_skill_vs_market",
            "edge_vs_market",
            "ece",
            "calibration_slope",
            "calibration_intercept",
            "auc",
            "reliability",
            "resolution",
            "base_rate",
        ):
            rows.append(
                (
                    "honesty",
                    STAND,
                    scope,
                    key,
                    getattr(r, key),
                    r.n_rows,
                    r.n_events,
                    None,
                    seg_meta if key == "per_event_brier" else "{}",
                )
            )

    # Слой 2 — куда ставить
    for r in rep.layer2:
        scope = f"{r.anchor_hours}h"
        for key in (
            "top1_per_event_accuracy",
            "precision_at_k",
            "precision_at_k_wilson_lo",
            "selected_mean_roi",
            "rejected_mean_roi",
            "selection_lift",
            "market_lift_brier",
        ):
            rows.append(
                (
                    "selection",
                    STAND,
                    scope,
                    key,
                    getattr(r, key),
                    r.precision_at_k_n,
                    None,
                    None,
                    "{}",
                )
            )

    # Слой 3 — когда (timing по horizon)
    for r in rep.layer3:
        scope = f"{r.anchor_hours}h:{r.horizon_bucket}"
        rows.append(
            (
                "timing",
                STAND,
                scope,
                "per_row_brier",
                r.per_row_brier,
                r.n,
                None,
                None,
                json.dumps({"base_rate": r.base_rate}),
            )
        )

    # Деньги — per (detector, category)
    for m in rep.money:
        meta = json.dumps(
            {
                "event_concentration": m.event_concentration,
                "loo_avg_roi_pct": m.loo_avg_roi_pct,
                "side_no_share": m.side_no_share,
                "bootstrap_roi_lo": m.bootstrap_roi_lo,
                "bootstrap_roi_hi": m.bootstrap_roi_hi,
                "median_roi_net_pct": m.median_roi_net_pct,
                "win_rate": m.win_rate,
                "artifact_reasons": m.artifact_reasons,
            },
            ensure_ascii=False,
        )
        rows.append(
            (
                "money",
                m.detector,
                m.category,
                "avg_roi_net_pct",
                m.avg_roi_net_pct,
                m.n_signals,
                m.n_events,
                m.reliability_flag,
                meta,
            )
        )
    return rows


async def run_ml_accuracy_snapshot() -> dict:
    """Cron entrypoint. Считает панель точности и пишет снимок в БД."""
    rep = await build_report()
    rows = _flatten(rep)
    if rows:
        await executemany(_INSERT_SQL, rows)
    n_artifacts = sum(1 for m in rep.money if m.reliability_flag == "ARTIFACT")
    logger.info(
        "ml_accuracy_snapshot_done",
        metrics_written=len(rows),
        anchors=len(rep.layer1),
        money_categories=len(rep.money),
        artifacts=n_artifacts,
    )
    return {"metrics_written": len(rows), "money_categories": len(rep.money), "artifacts": n_artifacts}
