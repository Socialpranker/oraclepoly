"""Bot Control Panel API — Pipeline V2 monitoring.

Phoenix V2 (2026-04-30): minimal post-cleanup endpoints. Только Pipeline V2
(detector_signals + pipeline_v2_shadow_positions) + system status.

Virtual mode (2026-05-01): pipeline_v2_shadow_positions теперь VIEW поверх
execution_orders WHERE execution_mode='virtual'. Старые SQL'ы работают
без изменений. См. migrations/20260501140001_compat_view_pipeline_v2_shadow_positions.sql.

Legacy endpoints (/strategies, /equity, /recent-fills, /live/*, /history) удалены.
Все данные теперь — через /v2/*.
"""

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.core.auth import check_admin
from app.core.cache import get_redis
from app.core.config import get_settings
from app.core.rate_limiter import limiter
from app.jobs.detector_signals_resolve import _signal_outcome
from app.services.database import fetch, fetchrow, records_to_list
from app.services.pnl_formulas import compute_gross_pnl, compute_net_pnl
from app.tasks.notifications import send_admin_message

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/status")
async def bot_status(_admin: dict = Depends(check_admin)):
    """Общий статус бота: execution flag, last virtual activity, alerts.

    Источник `pipeline_v2_shadow_positions` исторически назывался "shadow",
    но это reality — virtual paper-trading positions (детекторы в virtual stage).
    Ключи API переименованы соответственно: virtual_pnl_usd, last_virtual_at.

    "shadow" в lifecycle terminology — отдельный stage до virtual; смотри
    /v2/lifecycle/promotion_readiness для shadow gates.
    """
    settings = get_settings()

    last_virtual = await fetchrow(
        """
        SELECT MAX(opened_at) AS last_opened, COUNT(*) AS open_count
          FROM pipeline_v2_shadow_positions
         WHERE resolved = false
        """
    )

    pnl_row = await fetchrow(
        """
        SELECT
            COALESCE(SUM(COALESCE(net_pnl_usd, realized_pnl_usd)), 0) AS total_pnl,
            COUNT(*) FILTER (WHERE outcome = 'WIN') AS wins,
            COUNT(*) FILTER (WHERE outcome = 'LOSS') AS losses
          FROM pipeline_v2_shadow_positions
         WHERE resolved = true
        """
    )

    total_pnl = float(pnl_row["total_pnl"]) if pnl_row else 0.0
    wins = int(pnl_row["wins"]) if pnl_row else 0
    losses = int(pnl_row["losses"]) if pnl_row else 0
    n_resolved = wins + losses
    wr = round(wins / n_resolved, 4) if n_resolved > 0 else None

    alerts = []
    if not settings.execution_enabled:
        alerts.append({"level": "info", "message": "EXECUTION_ENABLED=false (virtual only)"})

    return {
        "status": "live" if settings.execution_enabled else "virtual",
        "execution_enabled": settings.execution_enabled,
        "virtual_pnl_usd": round(total_pnl, 2),
        "virtual_resolved": n_resolved,
        "virtual_open": int(last_virtual["open_count"]) if last_virtual else 0,
        "virtual_wr": wr,
        "last_virtual_at": (
            last_virtual["last_opened"].isoformat() if last_virtual and last_virtual["last_opened"] else None
        ),
        "alerts": alerts,
    }


# =============================================================================
# Pipeline V2 endpoints
# =============================================================================


@router.get("/v2/detectors")
async def bot_v2_detectors(_admin: dict = Depends(check_admin)):
    """Per-detector агрегаты: всё-историческое + recent (14d) + trend (vs prev 14d).

    Recent окно даёт «работает ли сейчас» (post-V2 + post side-aware fix),
    historical — общий контекст. Trend = recent_roi − prev_roi (pp). Verdict
    GREEN/YELLOW/RED для StatusTerminal/DetectorCard:
      GREEN  — recent_roi_net > 0% AND n_resolved >= 30
      RED    — recent_roi_net < -10% AND n_resolved >= 30
      YELLOW — иначе (мало данных или флэт около нуля).
    """
    historical = await fetch(
        """
        SELECT
            ds.detector,
            COUNT(*) AS n_total,
            COUNT(*) FILTER (WHERE ds.resolved = true) AS n_resolved,
            COUNT(*) FILTER (WHERE ds.outcome = 'WIN') AS n_wins,
            COUNT(*) FILTER (WHERE ds.outcome = 'LOSS') AS n_losses,
            COUNT(*) FILTER (WHERE ds.outcome = 'EXPIRED') AS n_expired,
            ROUND(
                AVG(ds.realized_roi_net) FILTER (WHERE ds.outcome IN ('WIN', 'LOSS'))::numeric,
                2
            ) AS avg_roi_pct,
            ROUND(AVG(ds.confidence)::numeric, 4) AS avg_confidence,
            MAX(ds.generated_at) AS last_emit_at
        FROM detector_signals ds
        GROUP BY ds.detector
        ORDER BY ds.detector
        """
    )

    recent = await fetch(
        """
        SELECT
            ds.detector,
            COUNT(*) AS n_total_recent,
            COUNT(*) FILTER (WHERE ds.outcome = 'WIN')  AS n_wins_recent,
            COUNT(*) FILTER (WHERE ds.outcome = 'LOSS') AS n_losses_recent,
            ROUND(
                AVG(ds.realized_roi_net) FILTER (WHERE ds.outcome IN ('WIN','LOSS'))::numeric,
                2
            ) AS recent_roi_net_pct
          FROM detector_signals ds
         WHERE ds.generated_at >= now() - interval '14 days'
         GROUP BY ds.detector
        """
    )
    recent_map = {r["detector"]: dict(r) for r in recent}

    prev = await fetch(
        """
        SELECT
            ds.detector,
            COUNT(*) FILTER (WHERE ds.outcome IN ('WIN','LOSS')) AS n_resolved_prev,
            ROUND(
                AVG(ds.realized_roi_net) FILTER (WHERE ds.outcome IN ('WIN','LOSS'))::numeric,
                2
            ) AS prev_roi_net_pct
          FROM detector_signals ds
         WHERE ds.generated_at >= now() - interval '28 days'
           AND ds.generated_at <  now() - interval '14 days'
         GROUP BY ds.detector
        """
    )
    prev_map = {r["detector"]: dict(r) for r in prev}

    cal_rows = await fetch(
        """
        SELECT detector, n_train, brier_raw, brier_holdout, fit_at
          FROM ml_calibrators
        """
    )
    cal_map = {r["detector"]: dict(r) for r in cal_rows}

    detectors = []
    for r in historical:
        d = dict(r)
        det = d["detector"]
        n_resolved = d["n_wins"] + d["n_losses"]
        d["wr"] = round(d["n_wins"] / n_resolved, 4) if n_resolved > 0 else None

        rec = recent_map.get(det) or {}
        n_wins_r = int(rec.get("n_wins_recent") or 0)
        n_losses_r = int(rec.get("n_losses_recent") or 0)
        n_resolved_r = n_wins_r + n_losses_r
        recent_roi = float(rec.get("recent_roi_net_pct") or 0.0) if rec.get("recent_roi_net_pct") is not None else None

        prv = prev_map.get(det) or {}
        prev_roi = float(prv.get("prev_roi_net_pct") or 0.0) if prv.get("prev_roi_net_pct") is not None else None

        if recent_roi is not None and n_resolved_r >= 30:
            if recent_roi > 0:
                verdict = "GREEN"
            elif recent_roi < -10:
                verdict = "RED"
            else:
                verdict = "YELLOW"
        else:
            verdict = "YELLOW"

        trend_pp = None
        if recent_roi is not None and prev_roi is not None:
            trend_pp = round(recent_roi - prev_roi, 2)

        d["recent_14d"] = {
            "n_total": int(rec.get("n_total_recent") or 0),
            "n_resolved": n_resolved_r,
            "n_wins": n_wins_r,
            "n_losses": n_losses_r,
            "wr": round(n_wins_r / n_resolved_r, 4) if n_resolved_r > 0 else None,
            "roi_net_pct": recent_roi,
            "trend_pp": trend_pp,
            "verdict": verdict,
        }

        cal = cal_map.get(det)
        d["calibrator"] = (
            {
                "fitted": True,
                "n_train": cal["n_train"],
                "brier_raw": float(cal["brier_raw"]),
                "brier_holdout": float(cal["brier_holdout"]),
                "fit_at": cal["fit_at"].isoformat() if cal["fit_at"] else None,
            }
            if cal
            else {"fitted": False}
        )
        detectors.append(d)

    # Lifecycle: per-stage агрегаты (4 stages plan)
    # Backtest: из markdown frontmatter (canonical predicted)
    # Shadow: signals/day за 7d
    # Virtual: PnL за 7d из execution_orders где execution_mode='virtual'
    # Live: PnL за 7d где execution_mode IN ('maker','taker')
    from app.services import detector_metrics
    from app.services.backtest_metadata import parse_backtest_md

    stage_map = await detector_metrics.fetch_stage_map()
    shadow_map = await detector_metrics.fetch_signals_per_day_7d()
    virt_pnl_map, live_pnl_map = await detector_metrics.fetch_pnl_by_mode_7d()

    for d in detectors:
        det = d["detector"]
        bt = parse_backtest_md(det) or {}
        d["lifecycle"] = {
            "stage": stage_map.get(det, "shadow"),
            "backtest": {
                "available": bool(bt.get("verdict")),
                "verdict": bt.get("verdict"),
                "honest_daily_usd_at_1k": bt.get("honest_daily_usd_at_1k"),
                "honest_daily_usd_at_5k": bt.get("honest_daily_usd_at_5k"),
                "wr_pct": bt.get("wr_pct"),
                "roi_net_pct": bt.get("roi_net_pct"),
            },
            "shadow": {
                "signals_per_day_7d": round(shadow_map.get(det, 0.0), 2),
            },
            "virtual": {
                "pnl_usd_7d": round(virt_pnl_map.get(det, 0.0), 2),
            },
            "live": {
                "pnl_usd_7d": round(live_pnl_map.get(det, 0.0), 2),
            },
        }

    return {"detectors": records_to_list(detectors)}


@router.get("/v2/honest-audit")
async def bot_v2_honest_audit(days: int = 30, _admin: dict = Depends(check_admin)):
    """Forward-валидация per (detector, category): honest PnL + вердикт + half-window тренд.

    Тот же расчёт, что еженедельный TG-термометр (detectors_honest_audit_report),
    но как структура для дашборда. SSoT логики — джоб; здесь только обёртка.
    """
    from app.jobs.detectors_honest_audit_report import (
        _AUDIT_SQL,
        _STAKE_USD,
        _half_per_day_1k,
        _trend_label,
        _verdict,
    )

    db_rows = await fetch(_AUDIT_SQL, days)
    half_days = days / 2.0
    rows: list[dict[str, Any]] = []
    for r in db_rows:
        n_dedup = int(r["n_dedup"] or 0)
        sum_roi_prop = float(r["sum_roi_prop"] or 0)
        first = r["first_signal"]
        last = r["last_signal"]
        span = max((last - first).days, 1) if first and last else 1
        per_day = sum_roi_prop * _STAKE_USD / span
        per_day_1k = per_day / 5.0

        # Тренд — ТОЧНО как джоб: _half_per_day_1k (@$1K) обеих половин в _trend_label.
        # Порог _TREND_DELTA_THRESHOLD тоже @$1K — единицы совпадают. SSoT логики = джоб.
        first_half = (
            _half_per_day_1k(float(r["sum_roi_prop_first"] or 0), half_days)
            if int(r["n_first_half"] or 0) > 0
            else None
        )
        second_half = (
            _half_per_day_1k(float(r["sum_roi_prop_second"] or 0), half_days)
            if int(r["n_second_half"] or 0) > 0
            else None
        )
        rows.append(
            {
                "detector": r["detector"],
                "category": r["category"],
                "n_dedup": n_dedup,
                "per_day": round(per_day, 2),
                "per_day_1k": per_day_1k,
                "trend": _trend_label(first_half, second_half),
                "verdict": _verdict(n_dedup, per_day_1k),
            }
        )
    rows.sort(key=lambda x: (x["detector"], -x["per_day_1k"]))
    return {"days": days, "rows": rows}


@router.get("/v2/model")
async def bot_v2_model(_admin: dict = Depends(check_admin)):
    """Active L2 LightGBM модель + drift status."""
    row = await fetchrow(
        """
        SELECT version, n_train, n_holdout, train_brier, holdout_brier, fit_at, is_active
          FROM ml_meta_labeler
         WHERE is_active = true
        """
    )
    history = await fetch(
        """
        SELECT version, holdout_brier, fit_at, is_active
          FROM ml_meta_labeler
         ORDER BY fit_at DESC
         LIMIT 10
        """
    )
    if not row:
        return {"active": None, "history": records_to_list(history)}

    return {
        "active": {
            "version": row["version"],
            "n_train": row["n_train"],
            "n_holdout": row["n_holdout"],
            "train_brier": float(row["train_brier"]),
            "holdout_brier": float(row["holdout_brier"]),
            "fit_at": row["fit_at"].isoformat() if row["fit_at"] else None,
        },
        "history": records_to_list(history),
    }


@router.get("/v2/detectors/{detector}/backtest")
async def bot_v2_detector_backtest(
    detector: str,
    _admin: dict = Depends(check_admin),
):
    """Canonical backtest снимок для детектора (parsed из docs/backtests/{detector}.md).

    Stage 1 в lifecycle: проверка гипотезы на 821 днях canonical HF parquet.
    Возвращает frontmatter + raw body_markdown + parsed sections для UI:
      hypothesis, filters, bucket_wr_table, category_breakdown, promotion_criteria.

    See docs/detector_lifecycle.md для полной диаграммы 4 stages.
    """
    from app.services.backtest_metadata import parse_backtest_md, parse_body_sections

    data = parse_backtest_md(detector)
    if data is None:
        return {
            "detector": detector,
            "available": False,
            "message": f"No canonical backtest found at docs/backtests/{detector}.md",
        }
    body = data.get("body_markdown", "")
    sections = parse_body_sections(body) if body else {}
    return {"detector": detector, "available": True, **data, "sections": sections}


@router.get("/v2/lifecycle/backtest_map")
async def bot_v2_lifecycle_backtest_map(_admin: dict = Depends(check_admin)):
    """Лёгкий dump всех backtest snapshots для hero scatter map в Backtest UI.

    Возвращает массив `items` — по одной точке на детектор. Содержит ключевые
    поля для 2D scatter (honest_haircut_factor × honest_daily_usd_at_5k) с
    color-coding по verdict + размер по trades_per_day.

    Включает stage из detector_state — UI рисует border цветом stage если
    detector уже promoted дальше Backtest.
    """
    from app.services.backtest_metadata import (
        list_available_backtests,
        parse_backtest_md,
    )

    state_rows = await fetch("SELECT detector, stage FROM detector_state")
    stage_map = {r["detector"]: r["stage"] for r in state_rows}

    items: list[dict] = []
    for det in list_available_backtests():
        meta = parse_backtest_md(det)
        if meta is None:
            continue
        items.append(
            {
                "detector": det,
                "verdict": meta.get("verdict"),
                "honest_daily_usd_at_5k": meta.get("honest_daily_usd_at_5k"),
                "honest_daily_usd_at_1k": meta.get("honest_daily_usd_at_1k"),
                "honest_haircut_factor": meta.get("honest_haircut_factor"),
                "trades_per_day": meta.get("trades_per_day"),
                "roi_net_pct": meta.get("roi_net_pct"),
                "wr_pct": meta.get("wr_pct"),
                "n_signals": meta.get("n_signals"),
                "backtest_days": meta.get("backtest_days"),
                "stage": stage_map.get(det),
            }
        )
    return {"items": items}


@router.get("/v2/lifecycle/counts")
async def bot_v2_lifecycle_counts(_admin: dict = Depends(check_admin)):
    """Per-stage детектор-counts для LifecycleProgress баннера в UI.

    Источники:
      backtest = MD files в docs/backtests/
      shadow   = COUNT(DISTINCT detector) в detector_signals за 7d
      virtual  = detector_state WHERE stage='virtual'
      live     = detector_state WHERE stage='live'

    See docs/detector_lifecycle.md.
    """
    from app.services.backtest_metadata import list_available_backtests

    backtest_total = len(list_available_backtests())

    shadow_row = await fetchrow(
        """
        SELECT COUNT(DISTINCT detector) AS n
          FROM detector_signals
         WHERE generated_at > now() - INTERVAL '7 days'
        """
    )
    shadow_total = int(shadow_row["n"]) if shadow_row else 0

    state_rows = await fetch(
        """
        SELECT stage, COUNT(*) AS n
          FROM detector_state
         GROUP BY stage
        """
    )
    by_stage = {r["stage"]: int(r["n"]) for r in state_rows}

    return {
        "backtest": {"total": backtest_total, "label": "MD файлов в docs/backtests/"},
        "shadow": {"total": shadow_total, "label": "детекторов эмитят signals 7d"},
        "virtual": {"total": by_stage.get("virtual", 0), "label": "promoted в virtual"},
        "live": {"total": by_stage.get("live", 0), "label": "promoted в live"},
    }


@router.get("/v2/detectors/{detector}/buckets")
async def bot_v2_detector_buckets(
    detector: str,
    _admin: dict = Depends(check_admin),
):
    """Per-bucket агрегаты для drill-down в UI таблице.

    Bucket = (category × zone). Возвращает за 7d:
      n_signals, n_resolved, wr_pct, roi_net_pct, virtual_pnl_7d, live_pnl_7d.

    Zone bucketing совпадает с /v2/dry/gate_status (lt40, 40-60, 60-80, 80-95, gte95_blocked).

    See docs/detector_lifecycle.md.
    """
    rows = await fetch(
        """
        WITH signals AS (
          SELECT
            ds.id,
            COALESCE(m.category, 'unknown') AS category,
            CASE
              WHEN ds.current_price < 0.40 THEN 'lt40'
              WHEN ds.current_price < 0.60 THEN '40-60'
              WHEN ds.current_price < 0.80 THEN '60-80'
              WHEN ds.current_price < 0.95 THEN '80-95'
              ELSE 'gte95_blocked'
            END AS zone,
            ds.resolved,
            ds.outcome,
            ds.realized_roi
          FROM detector_signals ds
          LEFT JOIN markets m ON m.polymarket_id = ds.market_id
          WHERE ds.detector = $1
            AND ds.generated_at > now() - INTERVAL '7 days'
        ),
        signal_agg AS (
          SELECT
            category,
            zone,
            COUNT(*) AS n_signals,
            COUNT(*) FILTER (WHERE resolved = true) AS n_resolved,
            COUNT(*) FILTER (WHERE outcome = 'WIN') AS n_wins,
            AVG(realized_roi) FILTER (WHERE resolved = true AND outcome IN ('WIN','LOSS')) AS avg_roi
          FROM signals
          GROUP BY category, zone
        ),
        order_agg AS (
          SELECT
            COALESCE(m.category, 'unknown') AS category,
            CASE
              WHEN eo.signal_price < 0.40 THEN 'lt40'
              WHEN eo.signal_price < 0.60 THEN '40-60'
              WHEN eo.signal_price < 0.80 THEN '60-80'
              WHEN eo.signal_price < 0.95 THEN '80-95'
              ELSE 'gte95_blocked'
            END AS zone,
            COALESCE(SUM(COALESCE(eo.net_pnl, eo.pnl)) FILTER (WHERE eo.execution_mode = 'virtual'), 0) AS virtual_pnl,
            COALESCE(SUM(COALESCE(eo.net_pnl, eo.pnl)) FILTER (WHERE eo.execution_mode IN ('maker','taker')), 0) AS live_pnl
          FROM execution_orders eo
          LEFT JOIN markets m ON m.polymarket_id = eo.market_id
          WHERE eo.detector = $1
            AND eo.created_at > now() - INTERVAL '7 days'
            -- Исключаем re-entry (см. standalone_virtual_pnl_inflation_2026-06-04).
            AND COALESCE((eo.meta->>'reentry_excluded')::bool, false) = false
          GROUP BY category, zone
        )
        SELECT
          s.category,
          s.zone,
          s.n_signals,
          s.n_resolved,
          s.n_wins,
          s.avg_roi,
          COALESCE(o.virtual_pnl, 0) AS virtual_pnl,
          COALESCE(o.live_pnl, 0) AS live_pnl
        FROM signal_agg s
        LEFT JOIN order_agg o ON o.category = s.category AND o.zone = s.zone
        ORDER BY s.n_signals DESC, s.category, s.zone
        """,
        detector,
    )

    buckets = []
    for r in rows:
        n_resolved = int(r["n_resolved"])
        n_wins = int(r["n_wins"])
        wr_pct = round(n_wins / n_resolved * 100, 1) if n_resolved > 0 else None
        avg_roi = float(r["avg_roi"]) if r["avg_roi"] is not None else None
        buckets.append(
            {
                "category": r["category"],
                "zone": r["zone"],
                "n_signals_7d": int(r["n_signals"]),
                "n_resolved_7d": n_resolved,
                "wr_pct": wr_pct,
                "roi_net_pct": round(avg_roi, 1) if avg_roi is not None else None,
                "virtual_pnl_7d": round(float(r["virtual_pnl"]), 2),
                "live_pnl_7d": round(float(r["live_pnl"]), 2),
            }
        )

    return {"detector": detector, "window_days": 7, "buckets": buckets}


@router.get("/v2/recent-signals")
async def bot_v2_recent_signals(
    limit: int = Query(50, ge=1, le=200),
    _admin: dict = Depends(check_admin),
):
    """Последние unresolved/recent signals с calibrated/meta values из shadow positions."""
    rows = await fetch(
        """
        SELECT
            p.signal_id,
            p.detector,
            p.market_id,
            p.side,
            p.raw_p,
            p.calibrated_p,
            p.meta_p,
            p.entry_price,
            p.ev_pct,
            p.stake_usd,
            p.opened_at,
            p.resolved,
            p.outcome,
            p.realized_roi_pct,
            p.realized_pnl_usd,
            ds.evidence->>'category' AS category
        FROM pipeline_v2_shadow_positions p
        LEFT JOIN detector_signals ds ON ds.id = p.signal_id
        ORDER BY p.opened_at DESC
        LIMIT $1
        """,
        limit,
    )
    return {"signals": records_to_list(rows)}


@router.get("/v2/signal-rate")
async def bot_v2_signal_rate(
    days: int = Query(7, ge=1, le=30),
    _admin: dict = Depends(check_admin),
):
    """Сигнал rate per detector — signals/hour, чтобы видеть "здоровье" pipeline."""
    rows = await fetch(
        """
        SELECT
            detector,
            COUNT(*) AS total_signals,
            COUNT(*) / GREATEST(1, EXTRACT(EPOCH FROM (now() - MIN(generated_at))) / 3600.0) AS rate_per_hour,
            MAX(generated_at) AS last_signal_at,
            EXTRACT(EPOCH FROM (now() - MAX(generated_at))) / 60.0 AS minutes_since_last
        FROM detector_signals
        WHERE generated_at > now() - make_interval(days => $1)
        GROUP BY detector
        ORDER BY total_signals DESC
        """,
        days,
    )

    detectors = []
    for r in rows:
        d = dict(r)
        d["total_signals"] = int(d["total_signals"])
        d["rate_per_hour"] = round(float(d["rate_per_hour"]), 2)
        d["minutes_since_last"] = round(float(d["minutes_since_last"]), 0)
        d["last_signal_at"] = d["last_signal_at"].isoformat() if d["last_signal_at"] else None
        d["is_stale"] = float(d["minutes_since_last"]) > 240
        detectors.append(d)

    return {"detectors": detectors, "days": days}


# Promotion gate thresholds per Правило 1 (docs/detector_lifecycle.md).
# required_n считается в EVENTS, не positions (per detector_audit-2026-05-01).
# Outcomes коррелированы внутри NegRisk events — n_positions переоценивает n.
PROMOTION_GATE_THRESHOLDS: dict[str, int] = {
    "D3_cross_mkt_arb": 30,  # structural edge, n меньше валидно (NegRisk math)
}


def _gate_state(
    n_resolved_events: int,
    threshold: int,
    distinct_events: int,
    distinct_days: int,
) -> str:
    """Lifecycle state machine из docs/detector_lifecycle.md.

    Использует n_resolved_events (не positions) per detector_audit-2026-05-01:
    outcomes коррелированы внутри events.
    """
    if n_resolved_events < 30:
        return "shadow_collecting"
    if n_resolved_events < threshold:
        return "shadow_watch"
    if distinct_events < 30 or distinct_days < 5:
        return "shadow_watch"  # statistical robustness
    return "gate_evaluating"


@router.get("/v2/gate-status")
async def bot_v2_gate_status(_admin: dict = Depends(check_admin)):
    """Promotion Gate status per detector — n_events/required_n + state machine.

    Primary metric: distinct_events (effective sample size).
    Реализует Промотион Гейт из docs/detector_lifecycle.md (Правило 1).
    """
    rows = await fetch(
        """
        SELECT
            p.detector,
            COUNT(*) AS n_total,
            COUNT(*) FILTER (WHERE p.resolved) AS n_resolved,
            COUNT(*) FILTER (WHERE p.outcome = 'WIN') AS n_wins,
            COUNT(*) FILTER (WHERE p.outcome = 'LOSS') AS n_losses,
            COUNT(DISTINCT p.market_id) FILTER (WHERE p.resolved) AS distinct_markets,
            COUNT(DISTINCT m.event_id) FILTER (WHERE p.resolved AND m.event_id IS NOT NULL) AS distinct_events,
            COUNT(DISTINCT DATE(p.opened_at)) FILTER (WHERE p.resolved) AS distinct_days,
            COALESCE(SUM(COALESCE(p.net_pnl_usd, p.realized_pnl_usd)) FILTER (WHERE p.resolved), 0) AS pnl_usd,
            COALESCE(SUM(p.stake_usd) FILTER (WHERE p.resolved), 0) AS staked_usd
        FROM pipeline_v2_shadow_positions p
        LEFT JOIN markets m ON m.condition_id = p.market_id OR m.polymarket_id = p.market_id
        GROUP BY p.detector
        ORDER BY p.detector
        """
    )

    out = []
    for r in rows:
        detector = r["detector"]
        n_resolved = int(r["n_resolved"])
        threshold = PROMOTION_GATE_THRESHOLDS.get(detector, 100)
        distinct_markets = int(r["distinct_markets"])
        distinct_events = int(r["distinct_events"])
        distinct_days = int(r["distinct_days"])
        wins = int(r["n_wins"])
        losses = int(r["n_losses"])
        terminal = wins + losses
        wr = round(wins / terminal, 4) if terminal > 0 else None
        pnl = float(r["pnl_usd"])
        staked = float(r["staked_usd"])
        roi_pct = round(pnl / staked * 100, 2) if staked > 0 else None

        # Primary metric: events, not positions
        progress_pct = round(min(100.0, distinct_events / threshold * 100), 1) if threshold > 0 else 100.0
        state = _gate_state(distinct_events, threshold, distinct_events, distinct_days)

        out.append(
            {
                "detector": detector,
                "n_total": int(r["n_total"]),
                "n_resolved": n_resolved,
                "n_wins": wins,
                "n_losses": losses,
                "wr": wr,
                "pnl_usd": round(pnl, 2),
                "roi_pct": roi_pct,
                "required_n": threshold,
                "progress_pct": progress_pct,
                "distinct_markets": distinct_markets,
                "distinct_events": distinct_events,
                "distinct_days": distinct_days,
                "state": state,
            }
        )

    return {"detectors": out}


@router.get("/v2/lifecycle/promotion_readiness")
async def bot_v2_lifecycle_promotion_readiness(_admin: dict = Depends(check_admin)):
    """Shadow → Virtual promotion readiness per detector.

    Возвращает два набора gates:

    Legacy (backward-compat для UI/Telegram):
      gate1_n_events   — distinct_events 7d ≥ 30
      gate2_wr_match   — recent WR в окне ±5pp от canonical
      gate3_verdict    — backtest verdict = PASS

    Honest v2 (см. docs/shadow_metrics_redesign_2026-05.md):
      G1 sample        — terminal ≥ 30
      G2 net PnL       — sum_roi_net_7d > 0 (fees+slippage учтены)
      G3 tail risk     — catastrophic_losses (<−80%) <5% от terminal
      G4 break-even WR — recent_wr > BE WR из virtual fingerprint
                         (или 50% fallback для shadow-only)

    Ready v2 = ≥3/4 gates passed. 4/4 = strong promote signal.
    """
    from app.services import detector_metrics

    shadow_map = await detector_metrics.fetch_shadow_signals_7d()
    stage_map = await detector_metrics.fetch_stage_map()
    backtest_map = detector_metrics.fetch_backtest_map()
    fingerprint_map = await detector_metrics.fetch_virtual_fingerprints()

    all_detectors = set(stage_map.keys()) | set(shadow_map.keys())

    out = []
    for det in sorted(all_detectors):
        gates = detector_metrics.compute_promotion_gates(
            shadow_data=shadow_map.get(det),
            backtest=backtest_map.get(det),
            fingerprint=fingerprint_map.get(det),
        )
        out.append({"detector": det, "stage": stage_map.get(det), **gates})

    return {"detectors": out}


# ─── Settle velocity (time from fill to settled) ─────────────────────────────
# Distribution buckets: <24h / 1-3d / 3-7d / >7d.
# Окно 30 дней — иначе старая backtest-история STANDALONE_V2_LGB задавит свежие.
# Threshold n_min=5: меньше → недостаточно данных для стат-распределения.
SETTLE_VELOCITY_WINDOW_DAYS = 30
SETTLE_VELOCITY_MIN_N = 5

_SETTLE_VELOCITY_SQL = """
SELECT
    detector,
    EXTRACT(EPOCH FROM (settled_at - filled_at)) / 3600.0 AS hours_to_settle
  FROM execution_orders
 WHERE status = 'virtual_settled'
   AND filled_at IS NOT NULL
   AND settled_at IS NOT NULL
   AND settled_at > now() - make_interval(days => $1)
   -- Исключаем re-entry: много быстрых STANDALONE settled скашивали распределение
   -- (см. standalone_virtual_pnl_inflation_2026-06-04).
   AND COALESCE((meta->>'reentry_excluded')::bool, false) = false
"""


def _bucketize_settle_hours(hours_list: list[float]) -> dict[str, float | int | None]:
    """Распределить hours_to_settle по корзинам 24h/1-3d/3-7d/>7d + median.

    Returns: {buckets: {under_24h, d1_3, d3_7, over_7d}, median_hours, n}
    Все bucket'ы — pct (0..100) от n_total. Если n < SETTLE_VELOCITY_MIN_N → None для buckets.
    """
    n = len(hours_list)
    if n == 0:
        return {"buckets": None, "median_hours": None, "n": 0}

    under_24 = sum(1 for h in hours_list if h < 24)
    d1_3 = sum(1 for h in hours_list if 24 <= h < 72)
    d3_7 = sum(1 for h in hours_list if 72 <= h < 168)
    over_7 = sum(1 for h in hours_list if h >= 168)

    if n < SETTLE_VELOCITY_MIN_N:
        return {"buckets": None, "median_hours": None, "n": n}

    sorted_h = sorted(hours_list)
    mid = n // 2
    median = sorted_h[mid] if n % 2 == 1 else (sorted_h[mid - 1] + sorted_h[mid]) / 2

    return {
        "buckets": {
            "under_24h_pct": round(under_24 / n * 100, 1),
            "d1_3_pct": round(d1_3 / n * 100, 1),
            "d3_7_pct": round(d3_7 / n * 100, 1),
            "over_7d_pct": round(over_7 / n * 100, 1),
        },
        "median_hours": round(median, 1),
        "n": n,
    }


async def _fetch_settle_velocity() -> tuple[dict[str, dict], dict]:
    """Вернуть (per_detector_map, global_summary) распределений времени до settled."""
    rows = await fetch(_SETTLE_VELOCITY_SQL, SETTLE_VELOCITY_WINDOW_DAYS)
    by_detector: dict[str, list[float]] = {}
    all_hours: list[float] = []
    for r in rows:
        h = float(r["hours_to_settle"])
        if h < 0:  # защита от мусорных данных
            continue
        by_detector.setdefault(r["detector"], []).append(h)
        all_hours.append(h)
    per_detector = {det: _bucketize_settle_hours(hs) for det, hs in by_detector.items()}
    global_summary = _bucketize_settle_hours(all_hours)
    global_summary["window_days"] = SETTLE_VELOCITY_WINDOW_DAYS
    return per_detector, global_summary


# ── Phase 0 Stage 5: PnL-weighted settle velocity ────────────────────────────

_SETTLE_VELOCITY_WEIGHTED_SQL = """
SELECT
    detector,
    EXTRACT(EPOCH FROM (settled_at - filled_at)) / 3600.0 AS hours_to_settle,
    COALESCE(stake, 0) AS stake_usd,
    ABS(COALESCE(net_pnl, 0)) AS abs_pnl_usd
  FROM execution_orders
 WHERE status = 'virtual_settled'
   AND filled_at IS NOT NULL
   AND settled_at IS NOT NULL
   AND settled_at > now() - make_interval(days => $1)
   -- Исключаем re-entry: PnL-вес (ABS net_pnl) раздут фантомом одного event
   -- (см. standalone_virtual_pnl_inflation_2026-06-04).
   AND COALESCE((meta->>'reentry_excluded')::bool, false) = false
"""


async def _fetch_settle_velocity_weighted() -> tuple[dict[str, dict], dict]:
    """PnL-weighted distribution per detector + global.

    Weight = abs(net_pnl) если > 0, иначе stake. abs(net_pnl) лучше отражает
    realized risk magnitude per сделка. См. docs/superpowers/plans/2026-05-honest-baseline/05-pnl-weighted-holding.md.
    """
    from app.services.settle_velocity_weighted import bucketize_settle_hours_weighted

    rows = await fetch(_SETTLE_VELOCITY_WEIGHTED_SQL, SETTLE_VELOCITY_WINDOW_DAYS)
    by_detector: dict[str, dict[str, list[float]]] = {}
    all_hours: list[float] = []
    all_weights: list[float] = []

    for r in rows:
        h = float(r["hours_to_settle"])
        if h < 0:
            continue
        abs_pnl = float(r["abs_pnl_usd"] or 0)
        stake = float(r["stake_usd"] or 0)
        weight = abs_pnl if abs_pnl > 0 else stake
        if weight <= 0:
            continue

        det = r["detector"]
        if det not in by_detector:
            by_detector[det] = {"hours": [], "weights": []}
        by_detector[det]["hours"].append(h)
        by_detector[det]["weights"].append(weight)
        all_hours.append(h)
        all_weights.append(weight)

    per_detector = {
        det: bucketize_settle_hours_weighted(data["hours"], data["weights"]) for det, data in by_detector.items()
    }
    global_summary = bucketize_settle_hours_weighted(all_hours, all_weights)
    global_summary["window_days"] = SETTLE_VELOCITY_WINDOW_DAYS
    return per_detector, global_summary


@router.get("/v2/lifecycle/live_readiness")
async def bot_v2_lifecycle_live_readiness(_admin: dict = Depends(check_admin)):
    """Virtual → Live readiness per detector.

    Возвращает два набора gates:

    Legacy `gates` (5, backward-compat для старого UI/order-level взгляда):
      pnl_positive       — order-level virtual pnl_usd_7d > $0 (paper-надут EARLY_EXIT)
      fill_rate          — virtual_filled / virtual_placed ≥ 50%
      adversarial_mirror — manual check (null если применимо)
      days_accumulated   — 14-28d virtual data
      execution_enabled  — env flag в .env

    Honest `gates_v2` (6, evaluate_promote_readiness — SSoT, общий с TG
    promote-callback app/api/telegram.py::_evaluate_and_handle_promote, ветка
    virtual→live). Phase 2 unification (audit: было расхождение — TG судил по
    фиксированному 7д-окну с высоким n_min, непроходимым для низкочастотных
    детекторов типа D3; UI уже использовал кумулятивный period. Теперь оба
    пути зовут одну функцию): N/wins/losses — кумулятивно от promoted_at
    (fetch_virtual_period_signals, без календарного потолка), net_pnl/Sortino/
    break-even WR — recency 30д-окно (fetch_recent_window_signals, ловит
    затухающий edge, который all-time период маскирует накопленной прибылью).
    Пороги/формулы — `evaluate_promote_gates`, см. [[shadow-roi-virtual-pnl]]:
      n_min              — signal-level terminal ≥ 30 (кумулятивно от promoted_at)
      pnl_positive       — recency 30d ROI > 0 AND Sortino > thr AND Wilson WR lower ≥ break-even WR
      fill_rate          — Wilson fill lower ≥ 50%
      adversarial_mirror — manual (mandatory: NULL → MANUAL_OVERRIDE_NEEDED)
      days_accumulated   — ≥14d virtual data
      execution_enabled  — env flag в .env
    `ready_v2` = decision == "READY" (ВСЕ 6 pass, hard AND — как TG). UI/карточка
    судят по gates_v2, TG и сервер теперь видят одну и ту же метрику.

    Также возвращает candidates_from_shadow — детекторы где Shadow 3/3 gates
    passed (готовы к Virtual). Используется в EmptyState когда 0 promoted.
    """
    settings = get_settings()
    from app.services import detector_metrics
    from app.services.portfolios import apply_gate_overrides, fetch_all_portfolios

    state_rows = await detector_metrics.fetch_virtual_state_rows()
    virtual_map = await detector_metrics.fetch_virtual_metrics_7d()
    # v5: signal-level экономика за весь virtual period (promoted_at → now()),
    # а не last-7d от NOW(). Фикс: shadow_7d не учитывал promoted_at → gate G0
    # мог fail/pass случайно в зависимости от активности именно последних 7d.
    shadow_map = await detector_metrics.fetch_virtual_period_signals(
        [(sr["detector"], sr["promoted_at"]) for sr in state_rows]
    )
    # evaluate_promote_readiness (Phase 2 unification): recency 30д-окно для
    # net_pnl/Sortino-компоненты gates_v2 — та же функция, что TG promote callback.
    recent_map = await detector_metrics.fetch_recent_window_signals([sr["detector"] for sr in state_rows])
    backtest_map = detector_metrics.fetch_backtest_map()

    # Phase 4: portfolio resolution для каждого detector + gate overrides.
    portfolio_map = await detector_metrics.fetch_portfolio_map()
    portfolios_by_id = {p.portfolio_id: p for p in await fetch_all_portfolios()}

    # 3b. Settle velocity distribution (30d rolling)
    settle_velocity_map, settle_velocity_global = await _fetch_settle_velocity()
    # Phase 0 Stage 5: PnL-weighted distribution в параллель к count-weighted
    settle_velocity_weighted_map, settle_velocity_weighted_global = await _fetch_settle_velocity_weighted()

    # 4. Build per-detector live_readiness items
    out = []
    for sr in state_rows:
        det = sr["detector"]
        promoted_at = sr["promoted_at"]
        days_virtual = (datetime.now(UTC) - promoted_at).days if promoted_at is not None else 0
        vd = virtual_map.get(det) or {}
        n_placed = int(vd.get("n_placed", 0) or 0)
        n_filled = int(vd.get("n_filled", 0) or 0)
        n_settled = int(vd.get("n_settled", 0) or 0)
        wins = int(vd.get("wins", 0) or 0)
        losses = int(vd.get("losses", 0) or 0)
        early_exits = int(vd.get("early_exits", 0) or 0)
        terminal = wins + losses
        pnl = float(vd.get("pnl_usd", 0.0) or 0.0)
        # Split PnL: resolved (WIN/LOSS = реальный edge) vs paper (EARLY_EXIT = mark-to-market).
        # Mark-to-market на mid-price в extreme zones завышает реальный exit value.
        pnl_resolved = float(vd.get("pnl_resolved_usd", 0.0) or 0.0)
        pnl_paper = float(vd.get("pnl_paper_usd", 0.0) or 0.0)
        fill_rate_pct = round(n_filled / n_placed * 100, 1) if n_placed > 0 else None
        wr_pct = round(wins / terminal * 100, 1) if terminal > 0 else None

        # v4: signal-level экономика (hold-to-resolution) — для honest promote-гейта.
        # Order-level pnl_usd надут paper EARLY_EXIT; signal realized_roi_net робастен.
        sd = shadow_map.get(det) or {}
        sig_sum_roi_net = float(sd.get("sum_roi_net_7d") or 0.0)
        sig_terminal = int(sd.get("terminal_7d") or 0)
        realized_roi_net_pct = round(sig_sum_roi_net, 1)

        bt = backtest_map.get(det)
        canonical_daily = bt.get("honest_daily_usd_at_5k") if bt else None
        projected_7d = canonical_daily * 7 if canonical_daily is not None else None

        # Gates
        gate_pnl = pnl > 0
        gate_fill = fill_rate_pct is not None and fill_rate_pct >= 50.0
        gate_adv = None  # manual / n/a unless backtest indicates
        # NB: backtest_map может содержать str 'partial' — это НЕ True/False.
        # Принимаем только настоящие bool (см. Phase 0 fix для D13_retail_*).
        if bt and isinstance(bt.get("adversarial_mirror_pass"), bool):
            gate_adv = bt["adversarial_mirror_pass"]
        gate_days = days_virtual >= 14
        gate_exec = settings.execution_enabled

        # Phase 4: build gates dict ПЕРЕД gates_passed/ready чтобы apply portfolio overrides.
        gates_dict: dict[str, dict[str, Any]] = {
            "pnl_positive": {
                "passed": gate_pnl,
                "value": round(pnl, 2),
                "label": "Virtual PnL > $0 net 7d",
            },
            "fill_rate": {
                "passed": gate_fill,
                "value": fill_rate_pct,
                "threshold": 50.0,
                "label": "Fill rate ≥ 50%",
            },
            "adversarial_mirror": {
                "passed": gate_adv,
                "value": "n/a" if gate_adv is None else "pass" if gate_adv else "fail",
                "label": "Adversarial mirror check",
            },
            "days_accumulated": {
                "passed": gate_days,
                "value": days_virtual,
                "threshold": 14,
                "label": "≥14d virtual data accumulated",
            },
            "execution_enabled": {
                "passed": gate_exec,
                "value": "true" if gate_exec else "false",
                "label": "EXECUTION_ENABLED=true",
            },
        }

        # Phase 4: apply portfolio gate overrides (e.g. ml portfolio skips adversarial_mirror).
        portfolio_id = portfolio_map.get(det)
        gate_overrides_applied: list[str] = []
        if portfolio_id is not None and portfolio_id in portfolios_by_id:
            p = portfolios_by_id[portfolio_id]
            if p.gate_overrides:
                gates_dict, gate_overrides_applied = apply_gate_overrides(gates_dict, p.gate_overrides)

        # Считаем gates_passed после overrides. None treated as pass (legacy).
        gate_results: list[bool | None] = [
            gates_dict["pnl_positive"]["passed"],
            gates_dict["fill_rate"]["passed"],
            gates_dict["adversarial_mirror"]["passed"]
            if gates_dict["adversarial_mirror"]["passed"] is not None
            else True,
            gates_dict["days_accumulated"]["passed"],
            gates_dict["execution_enabled"]["passed"],
        ]
        gates_passed = sum(1 for g in gate_results if g is True)
        gates_total = 5
        ready = all(g is True for g in gate_results)

        # Phase 0 Stage 3 — mandatory gates с Wilson lower bounds + N≥30.
        # Сосуществует с legacy gates_dict (UI compat).
        # Phase 2 (audit): gates_v2 строится через evaluate_promote_readiness —
        # SSoT, общий с TG promote-callback (app/api/telegram.py). N/wins/losses
        # кумулятивно от promoted_at (period=sd, уже получен выше для virtual_7d
        # display); net_pnl/Sortino/break-even WR — recency 30д-окно (recent_map),
        # НЕ весь period — иначе затухающий edge маскируется старой накопленной
        # прибылью (см. memory [[all-time-audit-hides-decaying-edge]]).
        adversarial_state_val = sr.get("adversarial_mirror_pass")
        # Fallback: если detector_state не имеет значения, читаем из backtest map
        # (legacy путь). Phase 0 Stage 3 follow-up — bulk-update через
        # POST /v2/lifecycle/adversarial_mirror и убрать fallback.
        #
        # NB: backtest_map может содержать str 'partial' — это НЕ True/False
        # (требует ручного решения). Принимаем только настоящие bool.
        if adversarial_state_val is None and bt:
            bt_adv = bt.get("adversarial_mirror_pass")
            if isinstance(bt_adv, bool):
                adversarial_state_val = bt_adv

        verdict_v2 = await detector_metrics.evaluate_promote_readiness(
            det,
            promoted_at,
            days_virtual=days_virtual,
            adversarial_pass=adversarial_state_val,
            execution_enabled=settings.execution_enabled,
            period=sd,
            recent=recent_map.get(det),
            virtual_metrics=vd,
        )
        # v2-derived counts для UI (карточка/map судят по v2, не legacy gates).
        # ready_v2 = decision == "READY" (hard AND по всем 6 — как TG promote,
        # telegram.py). а НЕ ≥threshold как в shadow→virtual promotion_readiness.
        gates_v2_passed = sum(1 for g in verdict_v2["gates"].values() if g.get("passed") is True)
        gates_v2_total = len(verdict_v2["gates"])
        ready_v2 = verdict_v2["decision"] == "READY"

        out.append(
            {
                "detector": det,
                "stage": sr["stage"],
                "portfolio_id": portfolio_id,
                "gate_overrides_applied": gate_overrides_applied,
                "promoted_at": promoted_at.isoformat() if promoted_at else None,
                "days_virtual": days_virtual,
                "virtual_7d": {
                    "n_placed": n_placed,
                    "n_filled": n_filled,
                    "n_settled": n_settled,
                    "fill_rate_pct": fill_rate_pct,
                    "pnl_usd": round(pnl, 2),
                    # PnL split (см. инцидент 2026-05-18):
                    #   pnl_resolved_usd — реально резолвнутые WIN/LOSS, "правда".
                    #   pnl_paper_usd    — EARLY_EXIT mark-to-market, может быть inflated
                    #                      в extreme zones из-за разницы mid vs realistic bid.
                    "pnl_resolved_usd": round(pnl_resolved, 2),
                    "pnl_paper_usd": round(pnl_paper, 2),
                    # v4: signal-level honest economics (hold-to-resolution) —
                    # ПЕРВИЧНАЯ метрика для оценки бойца (gate G1 судит по ней).
                    "realized_roi_net_pct": realized_roi_net_pct,
                    "sig_terminal": sig_terminal,
                    "wins": wins,
                    "losses": losses,
                    "early_exits": early_exits,
                    "wr_pct": wr_pct,
                },
                "canonical": {
                    "verdict": bt.get("verdict") if bt else None,
                    "honest_daily_usd_at_5k": canonical_daily,
                    "projected_7d_usd": round(projected_7d, 2) if projected_7d is not None else None,
                },
                "gates": gates_dict,
                "ready": ready,
                "gates_passed": gates_passed,
                "gates_total": gates_total,
                # Phase 0 Stage 3 — mandatory gates with Wilson + N min.
                # SSoT для UI: карточка/map судят по gates_v2 (signal-level), ровно
                # как TG promote virtual→live. ready_v2 = decision == "READY".
                "gates_v2": verdict_v2["gates"],
                "gates_decision_v2": verdict_v2["decision"],
                "ready_v2": ready_v2,
                "gates_v2_passed": gates_v2_passed,
                "gates_v2_total": gates_v2_total,
                "settle_velocity": settle_velocity_map.get(det, {"buckets": None, "median_hours": None, "n": 0}),
                # Phase 0 Stage 5 — PnL-weighted (hidden tail risk diagnostic)
                "settle_velocity_pnl_weighted": settle_velocity_weighted_map.get(det),
            }
        )

    # 5. Candidates from Shadow — где 3/3 gates passed (ready to promote → virtual).
    # Reuse logic from /v2/lifecycle/promotion_readiness.
    shadow_candidates = await _shadow_ready_candidates(state_rows)

    return {
        "detectors": out,
        "candidates_from_shadow": shadow_candidates,
        "execution_enabled": settings.execution_enabled,
        "settle_velocity_global": settle_velocity_global,
        # Phase 0 Stage 5
        "settle_velocity_global_pnl_weighted": settle_velocity_weighted_global,
    }


# ── Phase 0 Stage 3: Adversarial mirror evaluation endpoint ──────────────────


class AdversarialMirrorUpdate(BaseModel):
    """Payload для POST /v2/lifecycle/adversarial_mirror.

    Записывает результат manual adversarial mirror evaluation для detector.
    До этого момента `detector_state.adversarial_mirror_pass=NULL` →
    `evaluate_promote_gates` возвращает MANUAL_OVERRIDE_NEEDED (Stage 3+).
    """

    detector: str = Field(..., description="Detector identifier, e.g. 'D40_kalshi_cross_platform'")
    passed: bool = Field(
        ..., alias="pass", description="true=mirror failed (asymmetry confirmed) / false=mirror passed too (no edge)"
    )
    notes: str | None = Field(default=None, description="Optional rationale / methodology link")

    model_config = {"populate_by_name": True}


@router.post("/v2/lifecycle/adversarial_mirror")
@limiter.limit("60/minute")
async def bot_v2_set_adversarial_mirror(
    request: Request,
    payload: AdversarialMirrorUpdate,
    _admin: dict = Depends(check_admin),
):
    """Phase 0 Stage 3 — manual evaluation result.

    Adversarial mirror methodology: бэктест detector с inverted side. Если original
    PnL > 0 AND mirror PnL > 0 → fail (random / noise — pass=false). Если original
    PnL > 0 AND mirror PnL < 0 → pass (асимметрия = real edge — pass=true).

    После UPDATE адverarial gate перейдёт из not_evaluated → passed/fail, и
    evaluate_promote_gates сможет вернуть READY (вместо MANUAL_OVERRIDE_NEEDED).
    """

    result = await fetchrow(
        """
        UPDATE detector_state
           SET adversarial_mirror_pass = $2,
               adversarial_mirror_evaluated_at = NOW(),
               adversarial_mirror_notes = $3,
               updated_at = NOW()
         WHERE detector = $1
        RETURNING detector, adversarial_mirror_pass, adversarial_mirror_evaluated_at
        """,
        payload.detector,
        payload.passed,
        payload.notes,
    )
    if result is None:
        return {"ok": False, "error": f"detector_not_found:{payload.detector}"}
    return {
        "ok": True,
        "detector": result["detector"],
        "adversarial_mirror_pass": result["adversarial_mirror_pass"],
        "evaluated_at": result["adversarial_mirror_evaluated_at"].isoformat()
        if result["adversarial_mirror_evaluated_at"]
        else None,
    }


# Inform UI of pre-existing execute() symbol — kept for backward compat.
# (Помещаем после endpoint чтобы не нарушать import порядок.)


async def _shadow_ready_candidates(virtual_state_rows: list) -> list[dict]:
    """Helper: return detectors with Shadow 3/3 gates passed AND not yet in virtual.

    Reuses compute_promotion_gates() — single source of truth для gate logic.
    """
    from app.services import detector_metrics

    virtual_set = {r["detector"] for r in virtual_state_rows}
    shadow_map = await detector_metrics.fetch_shadow_signals_7d()
    backtest_map = detector_metrics.fetch_backtest_map()

    candidates = []
    for det, sd in shadow_map.items():
        if det in virtual_set:
            continue
        bt = backtest_map.get(det)
        gates = detector_metrics.compute_promotion_gates(shadow_data=sd, backtest=bt)
        if not gates["ready"]:
            continue
        candidates.append(
            {
                "detector": det,
                "shadow_gates_passed": 3,
                "distinct_events_7d": gates["shadow_7d"]["distinct_events"],
                "recent_wr_pct": gates["shadow_7d"]["recent_wr_pct"],
                "canonical_wr_pct": gates["canonical"]["canonical_wr_pct"],
                "honest_daily_usd_at_5k": gates["canonical"]["honest_daily_usd_at_5k"],
            }
        )
    return candidates


@router.get("/v2/daily_summary")
async def bot_v2_daily_summary(_admin: dict = Depends(check_admin)):
    """Простая дневная сводка: "сколько поставили, сколько выиграли, сколько проиграли".

    Возвращает массив дней за последние 14 дней. Для каждого:
      bets        — сколько ставок поставлено (virtual_placed/_filled/_settled)
      staked_usd  — суммарный stake
      won_usd     — сумма всех положительных net_pnl (выигрыши)
      lost_usd    — сумма всех отрицательных net_pnl (проигрыши, отрицательное)
      net_pnl     — won + lost (итог дня)
      n_settled   — резолвнуто ставок (WIN+LOSS+EARLY_EXIT)

    Прицел — UI показывает их в простом stat-блоке сверху Virtual Terminal.
    """
    rows = await fetch(
        """
        WITH by_day AS (
          SELECT
            DATE(created_at AT TIME ZONE 'UTC') AS day,
            COUNT(*) FILTER (
              WHERE execution_mode='virtual'
                AND status IN ('virtual_placed','virtual_filled','virtual_settled')
            ) AS bets,
            COALESCE(SUM(stake) FILTER (
              WHERE execution_mode='virtual'
                AND status IN ('virtual_placed','virtual_filled','virtual_settled')
            ), 0)::float AS staked_usd,
            COALESCE(SUM(COALESCE(net_pnl, pnl)) FILTER (
              WHERE execution_mode='virtual'
                AND status='virtual_settled'
                AND COALESCE(net_pnl, pnl) > 0
            ), 0)::float AS won_usd,
            COALESCE(SUM(COALESCE(net_pnl, pnl)) FILTER (
              WHERE execution_mode='virtual'
                AND status='virtual_settled'
                AND COALESCE(net_pnl, pnl) < 0
            ), 0)::float AS lost_usd,
            COUNT(*) FILTER (
              WHERE execution_mode='virtual' AND status='virtual_settled'
            ) AS n_settled
          FROM execution_orders
          WHERE created_at > now() - INTERVAL '14 days'
            -- Исключаем re-entry (meta.reentry_excluded=true) — иначе STANDALONE
            -- скальп одного event раздувает дашборд (~+$48K фантома vs flat).
            -- См. standalone_virtual_pnl_inflation_2026-06-04.
            AND COALESCE((meta->>'reentry_excluded')::bool, false) = false
          GROUP BY day
        )
        SELECT
          day::text,
          bets,
          ROUND(staked_usd::numeric, 2)::float AS staked_usd,
          ROUND(won_usd::numeric, 2)::float    AS won_usd,
          ROUND(lost_usd::numeric, 2)::float   AS lost_usd,
          ROUND((won_usd + lost_usd)::numeric, 2)::float AS net_pnl,
          n_settled
        FROM by_day
        ORDER BY day DESC
        """
    )
    days = [dict(r) for r in rows]
    totals = {
        "bets": sum(d["bets"] for d in days),
        "staked_usd": round(sum(d["staked_usd"] for d in days), 2),
        "won_usd": round(sum(d["won_usd"] for d in days), 2),
        "lost_usd": round(sum(d["lost_usd"] for d in days), 2),
        "net_pnl": round(sum(d["net_pnl"] for d in days), 2),
        "n_settled": sum(d["n_settled"] for d in days),
    }
    return {"days": days, "totals_14d": totals}


@router.get("/v2/portfolios")
async def bot_v2_portfolios(_admin: dict = Depends(check_admin)):
    """Portfolio rollup для UI tabs — bankroll, used/remaining exposure, kelly, n_detectors.

    Phase 4 of portfolios refactor. Источник правды для PortfolioTabs +
    PortfolioRollupCard (frontend Phase 5).

    Returns:
        {
          "portfolios": [{
            "portfolio_id", "display_name",
            "bankroll_usd", "exposure_cap_usd",
            "exposure_used_usd", "exposure_remaining_usd",
            "kelly_fraction", "gate_overrides",
            "n_detectors", "enabled"
          }, ...]
        }
    """
    from app.services.portfolios import fetch_all_portfolios

    portfolios = await fetch_all_portfolios()
    out: list[dict[str, Any]] = []
    for p in portfolios:
        used_row = await fetchrow(
            """
            SELECT COALESCE(SUM(eo.stake), 0)::float AS used,
                   (SELECT COUNT(*) FROM detector_state WHERE portfolio_id = $1) AS n_detectors
              FROM execution_orders eo
              JOIN detector_state ds ON ds.detector = eo.detector
             WHERE ds.portfolio_id = $1
               AND eo.execution_mode = 'virtual'
               AND eo.status IN ('virtual_placed', 'virtual_filled')
            """,
            p.portfolio_id,
        )
        used_usd = float(used_row["used"]) if used_row else 0.0
        n_detectors = int(used_row["n_detectors"]) if used_row else 0
        out.append(
            {
                "portfolio_id": p.portfolio_id,
                "display_name": p.display_name,
                "bankroll_usd": float(p.bankroll_usd),
                "exposure_cap_usd": float(p.exposure_cap_usd),
                "exposure_used_usd": round(used_usd, 2),
                "exposure_remaining_usd": round(max(0.0, float(p.exposure_cap_usd) - used_usd), 2),
                "kelly_fraction": float(p.kelly_fraction),
                "gate_overrides": p.gate_overrides,
                "n_detectors": n_detectors,
                "enabled": p.enabled,
            }
        )
    return {"portfolios": out}


@router.get("/v2/lifecycle/live_dashboard")
async def bot_v2_lifecycle_live_dashboard(_admin: dict = Depends(check_admin)):
    """Live Trading Floor — operational dashboard для реальных ордеров.

    Aggregates:
      - bankroll: wallet balance (live USDC) + initial bankroll + drawdown
      - equity_curve_7d: cumulative live PnL за 7d с virtual baseline overlay
      - alerts: EXECUTION_ENABLED, drawdown breaches, low balance
      - open_positions: filled orders без resolved outcome
      - fill_quality_7d: n_placed, n_filled, slippage, fee burn
      - detectors: per-detector live + slippage_delta_usd vs virtual projection

    Источники: execution_orders (mode IN ('maker','taker','virtual')), strategy_bankroll,
    wallet_resolver.fetch_balance() (с graceful fallback к initial_bankroll).
    """
    from datetime import date as date_cls
    from datetime import timedelta

    from app.services import detector_metrics

    settings = get_settings()

    # ----- 1. Bankroll: initial из strategy_bankroll, current через wallet_resolver -----
    initial_row = await fetchrow(
        """
        SELECT
            COALESCE(MAX(initial_bankroll), 0) AS initial_bankroll,
            COALESCE(MAX(bankroll), 0) AS bankroll
        FROM strategy_bankroll
        WHERE stage = 'live'
        """
    )
    initial_bankroll = float(initial_row["initial_bankroll"]) if initial_row else 0.0
    db_bankroll = float(initial_row["bankroll"]) if initial_row else 0.0

    # Live wallet balance — best-effort через wallet_resolver.
    wallet_balance: float | None = None
    try:
        from app.services import wallet_resolver

        wallet_balance = await wallet_resolver.fetch_balance()
    except Exception as e:  # noqa: BLE001
        logger.debug("wallet_resolver.fetch_balance() failed: %s", e)
        wallet_balance = None

    # Display balance: wallet если доступен, иначе db_bankroll, иначе initial.
    if wallet_balance is not None:
        display_balance = wallet_balance
    elif db_bankroll > 0:
        display_balance = db_bankroll
    else:
        display_balance = initial_bankroll

    drawdown_pct: float | None = None
    if initial_bankroll > 0:
        drawdown_pct = round((1 - display_balance / initial_bankroll) * 100, 2)

    kill_redis = await get_redis()
    kill_value = await kill_redis.get("bot:kill_switch") if kill_redis else None

    bankroll = {
        "wallet_balance_usd": round(display_balance, 2),
        "initial_bankroll_usd": round(initial_bankroll, 2),
        "drawdown_pct": drawdown_pct,
        "kill_switch_tripped": bool(kill_value),
        "kill_switch_reason": kill_value or None,
        "wallet_source": ("wallet" if wallet_balance is not None else "db" if db_bankroll > 0 else "initial_fallback"),
    }

    # ----- 2. Equity curve 7d: live cumulative + virtual baseline cumulative -----
    today = datetime.now(UTC).date()
    start_date = today - timedelta(days=6)
    daily_rows = await fetch(
        """
        SELECT
            (created_at AT TIME ZONE 'UTC')::date AS day,
            execution_mode,
            COALESCE(SUM(net_pnl), 0) AS pnl_settled
        FROM execution_orders
        WHERE created_at >= $1::timestamp
          AND status IN ('settled', 'virtual_settled')
          AND execution_mode IN ('maker', 'taker', 'virtual')
          -- Исключаем re-entry из virtual baseline (live-строки маркера не имеют →
          -- проходят). См. standalone_virtual_pnl_inflation_2026-06-04.
          AND COALESCE((meta->>'reentry_excluded')::bool, false) = false
        GROUP BY day, execution_mode
        ORDER BY day
        """,
        datetime.combine(start_date, datetime.min.time()),
    )
    by_day: dict[date_cls, dict[str, float]] = {}
    for r in daily_rows:
        d = r["day"]
        by_day.setdefault(d, {"live": 0.0, "virtual": 0.0})
        if r["execution_mode"] in ("maker", "taker"):
            by_day[d]["live"] += float(r["pnl_settled"])
        elif r["execution_mode"] == "virtual":
            by_day[d]["virtual"] += float(r["pnl_settled"])

    equity_curve = []
    live_cum = 0.0
    virt_cum = 0.0
    for i in range(7):
        d = start_date + timedelta(days=i)
        rec = by_day.get(d, {"live": 0.0, "virtual": 0.0})
        live_cum += rec["live"]
        virt_cum += rec["virtual"]
        equity_curve.append(
            {
                "date": d.isoformat(),
                "live_pnl_cum": round(live_cum, 2),
                "live_pnl_day": round(rec["live"], 2),
                "virtual_baseline_cum": round(virt_cum, 2),
                "virtual_baseline_day": round(rec["virtual"], 2),
            }
        )

    # ----- 3. Open positions (filled, не resolved) -----
    open_rows = await fetch(
        """
        SELECT
            detector, market_id, side, fill_price, stake, calibrated_p, created_at
        FROM execution_orders
        WHERE status IN ('filled', 'virtual_filled')
          AND outcome IS NULL
          AND execution_mode IN ('maker', 'taker', 'virtual')
        ORDER BY created_at DESC
        LIMIT 20
        """
    )
    open_list = []
    total_exposure = 0.0
    unrealized = 0.0
    for r in open_rows:
        stake = float(r["stake"]) if r["stake"] is not None else 0.0
        fill_price = float(r["fill_price"]) if r["fill_price"] is not None else None
        calibrated_p = float(r["calibrated_p"]) if r["calibrated_p"] is not None else None
        # Naive unrealized: (calibrated_p − fill_price) × shares × $1 payout assumption.
        unreal_pnl = 0.0
        if fill_price is not None and calibrated_p is not None and fill_price > 0:
            shares = stake / fill_price
            unreal_pnl = round(shares * (calibrated_p - fill_price), 2)
        total_exposure += stake
        unrealized += unreal_pnl
        open_list.append(
            {
                "detector": r["detector"],
                "market_id": r["market_id"],
                "side": r["side"],
                "entry_price": fill_price,
                "stake_usd": round(stake, 2),
                "unrealized_pnl_usd": unreal_pnl,
                "opened_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
        )
    exposure_pct = round(total_exposure / display_balance * 100, 2) if display_balance > 0 else None
    open_positions = {
        "count": len(open_list),
        "total_exposure_usd": round(total_exposure, 2),
        "exposure_pct_of_bankroll": exposure_pct,
        "unrealized_pnl_usd": round(unrealized, 2),
        "list": open_list,
    }

    # ----- 4. Fill quality 7d: live (maker/taker) только -----
    fq_row = await fetchrow(
        """
        SELECT
            COUNT(*) FILTER (WHERE status IN ('placed','filled','settled')) AS n_placed,
            COUNT(*) FILTER (WHERE status IN ('filled','settled')) AS n_filled,
            COALESCE(SUM(slippage_usd), 0) AS slippage_total,
            COALESCE(SUM(trading_fee_usd), 0) AS fee_total,
            COALESCE(SUM(net_pnl) FILTER (WHERE status = 'settled'), 0) AS pnl_total
        FROM execution_orders
        WHERE created_at > now() - INTERVAL '7 days'
          AND execution_mode IN ('maker', 'taker')
        """
    )
    n_placed_live = int(fq_row["n_placed"]) if fq_row else 0
    n_filled_live = int(fq_row["n_filled"]) if fq_row else 0
    slippage_total = float(fq_row["slippage_total"]) if fq_row else 0.0
    fee_total = float(fq_row["fee_total"]) if fq_row else 0.0
    pnl_total_live = float(fq_row["pnl_total"]) if fq_row else 0.0
    fill_rate_pct = round(n_filled_live / n_placed_live * 100, 1) if n_placed_live > 0 else None
    avg_slippage_pp = round(slippage_total / n_filled_live, 3) if n_filled_live > 0 else None
    fee_pct_of_pnl = round(abs(fee_total) / abs(pnl_total_live) * 100, 1) if pnl_total_live != 0 else None
    fill_quality = {
        "n_placed": n_placed_live,
        "n_filled": n_filled_live,
        "fill_rate_pct": fill_rate_pct,
        "avg_slippage_usd": avg_slippage_pp,
        "fee_burn_usd": round(fee_total, 2),
        "fee_burn_pct_of_pnl": fee_pct_of_pnl,
    }

    # ----- 5. Per-detector live breakdown с virtual projection -----
    det_state_rows = await fetch("SELECT detector FROM detector_state WHERE stage = 'live'")
    live_detectors_set = {r["detector"] for r in det_state_rows}

    backtest_map = detector_metrics.fetch_backtest_map()

    if live_detectors_set:
        det_rows = await fetch(
            """
            SELECT
                detector,
                COUNT(*) FILTER (WHERE status IN ('placed','filled','settled')) AS n_placed,
                COUNT(*) FILTER (WHERE status IN ('filled','settled')) AS n_filled,
                COUNT(*) FILTER (WHERE status='settled' AND outcome='WIN') AS wins,
                COUNT(*) FILTER (WHERE status='settled' AND outcome='LOSS') AS losses,
                COALESCE(SUM(net_pnl) FILTER (WHERE status='settled'), 0) AS pnl_usd,
                COALESCE(SUM(slippage_usd), 0) AS slippage_total,
                COALESCE(SUM(trading_fee_usd), 0) AS fee_total
            FROM execution_orders
            WHERE created_at > now() - INTERVAL '7 days'
              AND execution_mode IN ('maker','taker')
              AND detector = ANY($1)
            GROUP BY detector
            """,
            list(live_detectors_set),
        )
    else:
        det_rows = []
    det_live_map = {r["detector"]: r for r in det_rows}

    detectors_out = []
    for det in sorted(live_detectors_set):
        dlm = det_live_map.get(det)
        n_placed = int(dlm["n_placed"]) if dlm else 0
        n_filled = int(dlm["n_filled"]) if dlm else 0
        wins = int(dlm["wins"]) if dlm else 0
        losses = int(dlm["losses"]) if dlm else 0
        pnl = float(dlm["pnl_usd"]) if dlm else 0.0
        slippage = float(dlm["slippage_total"]) if dlm else 0.0
        fees = float(dlm["fee_total"]) if dlm else 0.0
        fr_pct = round(n_filled / n_placed * 100, 1) if n_placed > 0 else None

        bt = backtest_map.get(det)
        canonical_daily = bt.get("honest_daily_usd_at_5k") if bt else None
        # Projected scaled by current display balance (canonical at $5k baseline).
        projected_7d = None
        if canonical_daily is not None and initial_bankroll > 0:
            scale = display_balance / 5000.0
            projected_7d = round(canonical_daily * 7 * scale, 2)
        slippage_delta = round(pnl - projected_7d, 2) if projected_7d is not None else None

        detectors_out.append(
            {
                "detector": det,
                "live_7d": {
                    "n_placed": n_placed,
                    "n_filled": n_filled,
                    "wins": wins,
                    "losses": losses,
                    "pnl_usd": round(pnl, 2),
                    "fill_rate_pct": fr_pct,
                    "slippage_usd": round(slippage, 2),
                    "fees_usd": round(fees, 2),
                },
                "virtual_7d_projection_usd": projected_7d,
                "slippage_delta_usd": slippage_delta,
            }
        )

    # ----- 6. Alerts: EXECUTION_ENABLED + drawdown + balance + activity -----
    alerts = []
    if not settings.execution_enabled:
        alerts.append(
            {
                "level": "warning",
                "code": "EXEC_DISABLED",
                "message": "EXECUTION_ENABLED=false — никакие orders в CLOB не пойдут",
            }
        )
    if drawdown_pct is not None and drawdown_pct >= 5.0:
        alerts.append(
            {
                "level": "critical",
                "code": "DRAWDOWN_HIGH",
                "message": f"Drawdown {drawdown_pct}% >= 5% — проверить детекторы",
            }
        )
    elif drawdown_pct is not None and drawdown_pct >= 2.0:
        alerts.append(
            {
                "level": "warning",
                "code": "DRAWDOWN_MID",
                "message": f"Drawdown {drawdown_pct}% — следить",
            }
        )
    if display_balance < 1000 and display_balance > 0:
        alerts.append(
            {
                "level": "warning",
                "code": "LOW_BALANCE",
                "message": f"Wallet balance ${display_balance:.0f} < $1K — pause sizing?",
            }
        )
    if wallet_balance is None:
        alerts.append(
            {
                "level": "info",
                "code": "WALLET_UNAVAILABLE",
                "message": "wallet_resolver недоступен — показываем DB bankroll",
            }
        )
    if not detectors_out and settings.execution_enabled:
        alerts.append(
            {
                "level": "info",
                "code": "NO_LIVE_DETECTORS",
                "message": "EXECUTION_ENABLED=true, но 0 detectors в stage='live'",
            }
        )

    return {
        "bankroll": bankroll,
        "equity_curve_7d": equity_curve,
        "alerts": alerts,
        "open_positions": open_positions,
        "fill_quality_7d": fill_quality,
        "detectors": detectors_out,
        "execution_enabled": settings.execution_enabled,
    }


# =============================================================================
# Activity endpoints — day-centric metrics на execution_orders в virtual stage
# (см. _PLACED_STATUSES для статусов dry_run/virtual_*).
# =============================================================================


def _resolve_date(date: str | None):
    """date param из query → datetime.date UTC. Default = today UTC.

    Возвращаем datetime.date — asyncpg требует native type для ::date binding.
    """
    from datetime import UTC, datetime
    from datetime import date as date_cls

    if date:
        try:
            return date_cls.fromisoformat(date)
        except ValueError:
            return datetime.now(UTC).date()
    return datetime.now(UTC).date()


_PLACED_STATUSES = ("dry_run", "virtual_filled", "virtual_settled", "virtual_placed")
_SETTLED_STATUSES = ("dry_run", "virtual_filled", "virtual_settled")


@router.get("/v2/activity")
async def activity_summary(
    days: int = Query(14, ge=1, le=60),
    _admin: dict = Depends(check_admin),
):
    """Daily активность бота в virtual stage за N дней.

    SSoT для PnL settled orders — eo.net_pnl/pnl колонки (stored, посчитаны в
    v2_execution_resolve._compute_settlement_pnl). Counterfactual для rejected —
    через app/services/pnl_formulas.compute_net_pnl. Никаких inline формул.

    Filter: статусы dry_run/virtual_* (см. _PLACED_STATUSES). Live mode (filled/settled
    с execution_mode IN ('maker','taker')) сейчас в /v2/lifecycle/live_dashboard,
    в этот endpoint не попадает.
    """
    # eo.market_id хранит ДВА формата:
    #   - UUID (36) для старого dry_run (Pipeline V1) → markets.id
    #   - condition_id 0x... (66) для virtual_filled (Pipeline V2) → markets.polymarket_id
    # JOIN-условие через CASE по length, чтобы матчить оба.
    rows = await fetch(
        """
        SELECT
            DATE(eo.created_at) AS day,
            eo.status,
            eo.side,
            eo.stake,
            eo.signal_price,
            eo.pnl AS stored_pnl,
            eo.net_pnl AS stored_net_pnl,
            eo.outcome AS stored_outcome,
            eo.exit_reason AS stored_exit_reason,
            first_term.outcome AS winner,
            m.category
          FROM execution_orders eo
          LEFT JOIN markets m ON
             m.id = (CASE WHEN length(eo.market_id) = 36 THEN eo.market_id::uuid END)
          OR m.polymarket_id = (CASE WHEN length(eo.market_id) = 66 THEN eo.market_id END)
          LEFT JOIN LATERAL (
              SELECT outcome
                FROM oracle_snapshots os
               WHERE os.market_id = m.id
                 AND os.outcome IN ('yes','no')
               ORDER BY os.created_at ASC
               LIMIT 1
          ) first_term ON true
         WHERE eo.created_at >= now() - make_interval(days => $1)
           -- Исключаем re-entry (см. standalone_virtual_pnl_inflation_2026-06-04) —
           -- иначе per-day virtual P&L (activity summary) раздут скальпом одного event.
           AND COALESCE((eo.meta->>'reentry_excluded')::bool, false) = false
        """,
        days,
    )

    by_day: dict[str, dict] = {}
    for r in rows:
        d = r["day"].isoformat()
        bucket = by_day.setdefault(
            d,
            {
                "date": d,
                "attempted": 0,
                "rejected": 0,
                "placed": 0,
                "settled": 0,
                "wins": 0,
                "losses": 0,
                "early_exits": 0,
                "tp_exits": 0,
                "sl_exits": 0,
                "mid_zone_exits": 0,
                "pnl_net_usd": 0.0,
                "pnl_gross_usd": 0.0,
                "staked_usd": 0.0,
                "rejected_pnl_if_taken": 0.0,
            },
        )
        bucket["attempted"] += 1

        status = r["status"]
        side = r["side"]
        stake = float(r["stake"]) if r["stake"] is not None else 0.0
        price = float(r["signal_price"]) if r["signal_price"] is not None else 0.0
        winner = r["winner"]
        category = r["category"]
        stored_outcome = r["stored_outcome"]

        if status == "rejected":
            bucket["rejected"] += 1
            # Counterfactual — что было бы если бы взяли. Stored net_pnl нет
            # (rejected orders не settle'ятся), поэтому считаем через формулу.
            if winner and stake > 0 and price > 0:
                outcome = _signal_outcome(side, winner)
                if outcome:
                    bucket["rejected_pnl_if_taken"] += compute_net_pnl(
                        outcome=outcome,
                        side=side,
                        market_yes_price=price,
                        stake_usd=stake,
                        category=category,
                    )
            continue

        if status in _PLACED_STATUSES:
            bucket["placed"] += 1
            bucket["staked_usd"] += stake

            # Early-exit (TP/SL/mid_zone_exit) path: position_manager записал
            # outcome='EARLY_EXIT' + stored_net_pnl. Не зависит от winner.
            if stored_outcome == "EARLY_EXIT":
                bucket["settled"] += 1
                bucket["early_exits"] += 1
                # Категоризация: точная по exit_reason (mid_zone_exit отдельно).
                exit_reason = r["stored_exit_reason"]
                stored_net = r["stored_net_pnl"]
                if stored_net is not None:
                    pnl_val = float(stored_net)
                    bucket["pnl_net_usd"] += pnl_val
                    if exit_reason == "take_profit":
                        bucket["tp_exits"] += 1
                    elif exit_reason == "stop_loss":
                        bucket["sl_exits"] += 1
                    elif exit_reason == "mid_zone_exit":
                        bucket["mid_zone_exits"] += 1
                    elif pnl_val > 0:
                        # Fallback для legacy rows без exit_reason
                        bucket["tp_exits"] += 1
                    else:
                        bucket["sl_exits"] += 1
                stored_gross = r["stored_pnl"]
                if stored_gross is not None:
                    bucket["pnl_gross_usd"] += float(stored_gross)
                continue

            if status in _SETTLED_STATUSES and winner and stake > 0 and price > 0:
                outcome = _signal_outcome(side, winner)
                if outcome:
                    bucket["settled"] += 1
                    if outcome == "WIN":
                        bucket["wins"] += 1
                    else:
                        bucket["losses"] += 1
                    # SSoT: settled orders читают stored eo.net_pnl/pnl (посчитаны в
                    # v2_execution_resolve). Если NULL (старый dry_run без stored value)
                    # — fallback на counterfactual формулу.
                    stored_net = r["stored_net_pnl"]
                    stored_gross = r["stored_pnl"]
                    if stored_net is not None:
                        bucket["pnl_net_usd"] += float(stored_net)
                    else:
                        bucket["pnl_net_usd"] += compute_net_pnl(
                            outcome=outcome,
                            side=side,
                            market_yes_price=price,
                            stake_usd=stake,
                            category=category,
                        )
                    if stored_gross is not None:
                        bucket["pnl_gross_usd"] += float(stored_gross)
                    else:
                        bucket["pnl_gross_usd"] += compute_gross_pnl(
                            outcome=outcome,
                            side=side,
                            market_yes_price=price,
                            stake_usd=stake,
                        )

    daily = []
    for d in sorted(by_day.keys()):
        b = by_day[d]
        daily.append(
            {
                "date": b["date"],
                "attempted": b["attempted"],
                "rejected": b["rejected"],
                "placed": b["placed"],
                "settled": b["settled"],
                "wins": b["wins"],
                "losses": b["losses"],
                "early_exits": b["early_exits"],
                "tp_exits": b["tp_exits"],
                "sl_exits": b["sl_exits"],
                "mid_zone_exits": b["mid_zone_exits"],
                "pnl_net_usd": round(b["pnl_net_usd"], 2),
                "pnl_gross_usd": round(b["pnl_gross_usd"], 2),
                "staked_usd": round(b["staked_usd"], 2),
                "rejected_pnl_if_taken": round(b["rejected_pnl_if_taken"], 2),
            }
        )

    summary = {
        "days": days,
        "total_attempted": sum(d["attempted"] for d in daily),
        "total_rejected": sum(d["rejected"] for d in daily),
        "total_placed": sum(d["placed"] for d in daily),
        "total_settled": sum(d["settled"] for d in daily),
        "total_wins": sum(d["wins"] for d in daily),
        "total_losses": sum(d["losses"] for d in daily),
        "total_early_exits": sum(d["early_exits"] for d in daily),
        "total_tp_exits": sum(d["tp_exits"] for d in daily),
        "total_sl_exits": sum(d["sl_exits"] for d in daily),
        "total_mid_zone_exits": sum(d["mid_zone_exits"] for d in daily),
        "total_pnl_net_usd": round(sum(d["pnl_net_usd"] for d in daily), 2),
        "total_pnl_gross_usd": round(sum(d["pnl_gross_usd"] for d in daily), 2),
        "total_staked_usd": round(sum(d["staked_usd"] for d in daily), 2),
        "total_rejected_pnl_if_taken": round(sum(d["rejected_pnl_if_taken"] for d in daily), 2),
    }
    return {"summary": summary, "daily": daily}


@router.get("/v2/activity/day/{date}")
async def activity_day(
    date: str,
    _admin: dict = Depends(check_admin),
):
    """Drill-down: список всех ордеров (placed + rejected) за конкретный день.

    Каждая строка — отдельный ордер. Settled net_pnl читается из execution_orders.net_pnl
    (SSoT). Counterfactual для rejected — через compute_net_pnl.
    """
    rows = await fetch(
        """
        SELECT
            eo.id,
            eo.created_at,
            eo.detector,
            eo.strategy_key,
            eo.market_id,
            eo.side,
            eo.stake,
            eo.signal_price,
            eo.status,
            eo.cancel_reason,
            eo.rejection_reason,
            eo.net_pnl AS stored_net_pnl,
            m.question,
            m.category,
            first_term.outcome AS winner
          FROM execution_orders eo
          LEFT JOIN markets m ON
             m.id = (CASE WHEN length(eo.market_id) = 36 THEN eo.market_id::uuid END)
          OR m.polymarket_id = (CASE WHEN length(eo.market_id) = 66 THEN eo.market_id END)
          LEFT JOIN LATERAL (
              SELECT outcome
                FROM oracle_snapshots os
               WHERE os.market_id = m.id
                 AND os.outcome IN ('yes','no')
               ORDER BY os.created_at ASC
               LIMIT 1
          ) first_term ON true
         WHERE DATE(eo.created_at) = $1::date
           -- Исключаем re-entry (см. standalone_virtual_pnl_inflation_2026-06-04) —
           -- чтобы day drill-down и его by_detector сводка совпадали с дневным P&L
           -- (иначе суммы не сойдутся: список покажет фантом, а activity — нет).
           AND COALESCE((eo.meta->>'reentry_excluded')::bool, false) = false
         ORDER BY eo.created_at DESC
         LIMIT 500
        """,
        date,
    )

    items = []
    by_detector: dict[str, dict] = {}
    by_reason: dict[str, int] = {}

    for r in rows:
        side = r["side"]
        stake = float(r["stake"]) if r["stake"] is not None else 0.0
        price = float(r["signal_price"]) if r["signal_price"] is not None else 0.0
        winner = r["winner"]
        category = r["category"]
        det = r["detector"] or r["strategy_key"] or "unknown"
        status = r["status"]
        reason = r["rejection_reason"] or r["cancel_reason"]

        outcome = None
        net_pnl = None
        if winner and price > 0:
            outcome_str = _signal_outcome(side, winner)
            if outcome_str:
                outcome = outcome_str
                stored = r["stored_net_pnl"]
                if stored is not None:
                    net_pnl = round(float(stored), 2)
                else:
                    net_pnl = round(
                        compute_net_pnl(
                            outcome=outcome_str,
                            side=side,
                            market_yes_price=price,
                            stake_usd=stake,
                            category=category,
                        ),
                        2,
                    )

        items.append(
            {
                "id": str(r["id"]),
                "ts": r["created_at"].isoformat() if r["created_at"] else None,
                "detector": r["detector"],
                "strategy_key": r["strategy_key"],
                "market_id": r["market_id"],
                "question": r["question"],
                "category": category,
                "side": side,
                "stake_usd": round(stake, 2),
                "signal_price": round(price, 4),
                "status": status,
                "cancel_reason": r["cancel_reason"],
                "rejection_reason": r["rejection_reason"],
                "outcome": outcome,
                "net_pnl_usd": net_pnl,
            }
        )

        # Per-detector aggregation
        bucket = by_detector.setdefault(
            det,
            {
                "detector": det,
                "placed": 0,
                "rejected": 0,
                "settled": 0,
                "wins": 0,
                "losses": 0,
                "stake_usd": 0.0,
                "pnl_net_usd": 0.0,
            },
        )
        if status == "rejected":
            bucket["rejected"] += 1
        elif status in _PLACED_STATUSES:
            bucket["placed"] += 1
            bucket["stake_usd"] += stake
            if net_pnl is not None:
                bucket["settled"] += 1
                bucket["pnl_net_usd"] += net_pnl
                if outcome == "WIN":
                    bucket["wins"] += 1
                else:
                    bucket["losses"] += 1

        # Per-rejection-reason aggregation
        if status == "rejected" and reason:
            by_reason[reason] = by_reason.get(reason, 0) + 1

    detectors = []
    for det in sorted(by_detector.keys()):
        b = by_detector[det]
        wr = round(b["wins"] / b["settled"], 4) if b["settled"] > 0 else None
        roi = round(b["pnl_net_usd"] / b["stake_usd"] * 100, 2) if b["stake_usd"] > 0 else None
        detectors.append(
            {
                "detector": b["detector"],
                "placed": b["placed"],
                "rejected": b["rejected"],
                "settled": b["settled"],
                "wins": b["wins"],
                "losses": b["losses"],
                "wr": wr,
                "stake_usd": round(b["stake_usd"], 2),
                "pnl_net_usd": round(b["pnl_net_usd"], 2),
                "roi_net_pct": roi,
            }
        )

    rejections = [{"reason": k, "count": v} for k, v in sorted(by_reason.items(), key=lambda x: x[1], reverse=True)]

    return {
        "date": date,
        "count": len(items),
        "items": items,
        "by_detector": detectors,
        "by_rejection": rejections,
    }


@router.get("/v2/activity/calendar")
async def activity_calendar(
    days: int = Query(60, ge=14, le=180),
    _admin: dict = Depends(check_admin),
):
    """Per-day календарь для heat-map за N дней (default 60).

    SSoT для settled PnL — execution_orders.net_pnl (stored). Counterfactual для
    rejected ordersне считается в этом endpoint (только placed/settled).
    """
    rows = await fetch(
        """
        SELECT
            DATE(eo.created_at) AS day,
            eo.status,
            eo.side,
            eo.stake,
            eo.signal_price,
            eo.net_pnl AS stored_net_pnl,
            first_term.outcome AS winner,
            m.category
          FROM execution_orders eo
          LEFT JOIN markets m ON
             m.id = (CASE WHEN length(eo.market_id) = 36 THEN eo.market_id::uuid END)
          OR m.polymarket_id = (CASE WHEN length(eo.market_id) = 66 THEN eo.market_id END)
          LEFT JOIN LATERAL (
              SELECT outcome
                FROM oracle_snapshots os
               WHERE os.market_id = m.id
                 AND os.outcome IN ('yes','no')
               ORDER BY os.created_at ASC
               LIMIT 1
          ) first_term ON true
         WHERE eo.created_at >= now() - make_interval(days => $1)
           -- Исключаем re-entry (см. standalone_virtual_pnl_inflation_2026-06-04) —
           -- иначе calendar heat-map per-day P&L раздут скальпом одного event.
           AND COALESCE((eo.meta->>'reentry_excluded')::bool, false) = false
        """,
        days,
    )

    by_day: dict[str, dict] = {}
    for r in rows:
        d = r["day"].isoformat()
        bucket = by_day.setdefault(
            d,
            {
                "date": d,
                "placed": 0,
                "rejected": 0,
                "settled": 0,
                "wins": 0,
                "pnl_net_usd": 0.0,
            },
        )
        status = r["status"]
        if status == "rejected":
            bucket["rejected"] += 1
            continue
        if status in _PLACED_STATUSES:
            bucket["placed"] += 1
            if status in _SETTLED_STATUSES and r["winner"]:
                stake = float(r["stake"]) if r["stake"] is not None else 0.0
                price = float(r["signal_price"]) if r["signal_price"] is not None else 0.0
                if stake > 0 and price > 0:
                    outcome = _signal_outcome(r["side"], r["winner"])
                    if outcome:
                        bucket["settled"] += 1
                        if outcome == "WIN":
                            bucket["wins"] += 1
                        # SSoT: stored eo.net_pnl, fallback на формулу для legacy dry_run
                        stored = r["stored_net_pnl"]
                        if stored is not None:
                            bucket["pnl_net_usd"] += float(stored)
                        else:
                            bucket["pnl_net_usd"] += compute_net_pnl(
                                outcome=outcome,
                                side=r["side"],
                                market_yes_price=price,
                                stake_usd=stake,
                                category=r["category"],
                            )

    days_list = []
    for d in sorted(by_day.keys()):
        b = by_day[d]
        days_list.append(
            {
                "date": b["date"],
                "placed": b["placed"],
                "rejected": b["rejected"],
                "settled": b["settled"],
                "wins": b["wins"],
                "pnl_net_usd": round(b["pnl_net_usd"], 2),
            }
        )

    return {"days": days, "items": days_list}


@router.get("/v2/ensemble/opportunities")
async def ensemble_opportunities(_admin: dict = Depends(check_admin)):
    """Pairs of detectors that historically agreed on (market, side) — backtest WR/ROI."""
    rows = await fetch(
        """
        WITH pairs AS (
          SELECT a.market_id, a.side,
                 a.detector AS det_a, b.detector AS det_b,
                 MAX(a.outcome) AS outcome,
                 MAX(a.realized_roi) AS roi
          FROM detector_signals a
          JOIN detector_signals b
            ON a.market_id = b.market_id AND a.side = b.side
           AND a.detector < b.detector
          WHERE a.resolved = true AND a.outcome IN ('WIN','LOSS')
            AND b.resolved = true AND b.outcome IN ('WIN','LOSS')
          GROUP BY a.market_id, a.side, a.detector, b.detector
        )
        SELECT
            det_a, det_b,
            COUNT(*) AS markets,
            COUNT(*) FILTER (WHERE outcome = 'WIN') AS wins,
            ROUND(100.0 * COUNT(*) FILTER (WHERE outcome = 'WIN') / COUNT(*), 1) AS wr_pct,
            ROUND(
                (SUM(CASE WHEN outcome='WIN' THEN roi ELSE -100 END) / COUNT(*))::numeric,
                1
            ) AS net_roi_pct
        FROM pairs
        GROUP BY det_a, det_b
        HAVING COUNT(*) >= 100
        ORDER BY markets DESC
        LIMIT 20
        """
    )
    return {
        "pairs": [
            {
                "det_a": r["det_a"],
                "det_b": r["det_b"],
                "markets": int(r["markets"]),
                "wins": int(r["wins"]),
                "wr_pct": float(r["wr_pct"]),
                "net_roi_pct": float(r["net_roi_pct"]),
            }
            for r in rows
        ],
    }


# Whitelist buckets для readiness gate — 4 (detector, category, entry_zone)
# тройки которые исторически показывали edge. Используется только в /v2/dry/gate_status.
WHITELIST_BUCKETS: list[tuple[str, str, str]] = []


@router.get("/v2/dry/gate_status")
async def dry_gate_status(_admin: dict = Depends(check_admin)):
    """Aggregating gate dashboard. 6 critериев → verdict ready/not_ready."""
    results = []
    for det, cat, zone in WHITELIST_BUCKETS:
        criteria = {}
        ev_row = await fetchrow(
            """
            WITH bucketed AS (
              SELECT ds.event_id, MAX(CASE WHEN ds.outcome='WIN' THEN 1 ELSE 0 END) AS won
                FROM detector_signals ds LEFT JOIN markets m ON m.polymarket_id = ds.market_id
               WHERE ds.detector = $1 AND COALESCE(m.category,'unknown') = $2
                 AND CASE WHEN ds.current_price < 0.40 THEN 'lt40'
                          WHEN ds.current_price < 0.60 THEN '40-60'
                          WHEN ds.current_price < 0.80 THEN '60-80'
                          WHEN ds.current_price < 0.95 THEN '80-95'
                          ELSE 'gte95_blocked' END = $3
                 AND ds.resolved=true AND ds.outcome IN ('WIN','LOSS') AND ds.source='live_v2'
                 AND ds.generated_at > now() - INTERVAL '30 days'
               GROUP BY 1
            ),
            sigs AS (
              SELECT COUNT(*) AS n_signals, SUM(CASE WHEN ds.outcome='WIN' THEN 1 ELSE 0 END) AS n_wins
                FROM detector_signals ds LEFT JOIN markets m ON m.polymarket_id = ds.market_id
               WHERE ds.detector = $1 AND COALESCE(m.category,'unknown') = $2
                 AND CASE WHEN ds.current_price < 0.40 THEN 'lt40'
                          WHEN ds.current_price < 0.60 THEN '40-60'
                          WHEN ds.current_price < 0.80 THEN '60-80'
                          WHEN ds.current_price < 0.95 THEN '80-95'
                          ELSE 'gte95_blocked' END = $3
                 AND ds.resolved=true AND ds.outcome IN ('WIN','LOSS') AND ds.source='live_v2'
                 AND ds.generated_at > now() - INTERVAL '30 days'
            )
            SELECT
              (SELECT COUNT(*) FROM bucketed) AS n_events,
              (SELECT SUM(won) FROM bucketed) AS n_wins_events,
              (SELECT n_signals FROM sigs) AS n_signals,
              (SELECT n_wins FROM sigs) AS n_wins_signals
            """,
            det,
            cat,
            zone,
        )
        n_ev = int(ev_row["n_events"] or 0) if ev_row else 0
        n_sig = int(ev_row["n_signals"] or 0) if ev_row else 0
        n_wins_ev = int(ev_row["n_wins_events"] or 0) if ev_row else 0
        n_wins_sig = int(ev_row["n_wins_signals"] or 0) if ev_row else 0
        criteria["n_events_30d"] = {"value": n_ev, "threshold": 30, "pass": n_ev >= 30}
        wr_sig = (n_wins_sig / n_sig * 100) if n_sig > 0 else 0
        wr_ev = (n_wins_ev / n_ev * 100) if n_ev > 0 else 0
        inflation = (wr_sig / wr_ev) if wr_ev > 0 else None
        criteria["inflation_factor"] = {
            "value": round(inflation, 2) if inflation else None,
            "threshold": 2.0,
            "pass": inflation is not None and inflation < 2.0,
        }
        drift_row = await fetchrow(
            """
            WITH bucketed AS (
              SELECT ds.outcome, ds.resolved_at FROM detector_signals ds
              LEFT JOIN markets m ON m.polymarket_id = ds.market_id
              WHERE ds.detector = $1 AND COALESCE(m.category,'unknown') = $2
                AND CASE WHEN ds.current_price < 0.40 THEN 'lt40'
                         WHEN ds.current_price < 0.60 THEN '40-60'
                         WHEN ds.current_price < 0.80 THEN '60-80'
                         WHEN ds.current_price < 0.95 THEN '80-95'
                         ELSE 'gte95_blocked' END = $3
                AND ds.resolved=true AND ds.outcome IN ('WIN','LOSS') AND ds.source='live_v2'
            )
            SELECT
              AVG(CASE WHEN outcome='WIN' THEN 1.0 ELSE 0 END) FILTER (WHERE resolved_at > now() - INTERVAL '7 days') AS wr_recent,
              AVG(CASE WHEN outcome='WIN' THEN 1.0 ELSE 0 END) FILTER (WHERE resolved_at > now() - INTERVAL '30 days') AS wr_baseline
            FROM bucketed
            """,
            det,
            cat,
            zone,
        )
        wr_r = float(drift_row["wr_recent"] or 0) if drift_row else 0
        wr_b = float(drift_row["wr_baseline"] or 0) if drift_row else 0
        drift_wr_pp = round((wr_r - wr_b) * 100, 1)
        criteria["drift_wr_pp"] = {"value": drift_wr_pp, "threshold": -5, "pass": drift_wr_pp >= -5}

        hf_row = await fetchrow(
            """
            WITH bucketed AS (
              SELECT ds.outcome, ds.source FROM detector_signals ds
              LEFT JOIN markets m ON m.polymarket_id = ds.market_id
              WHERE ds.detector = $1 AND COALESCE(m.category,'unknown') = $2
                AND CASE WHEN ds.current_price < 0.40 THEN 'lt40'
                         WHEN ds.current_price < 0.60 THEN '40-60'
                         WHEN ds.current_price < 0.80 THEN '60-80'
                         WHEN ds.current_price < 0.95 THEN '80-95'
                         ELSE 'gte95_blocked' END = $3
                AND ds.resolved=true AND ds.outcome IN ('WIN','LOSS')
            )
            SELECT
              AVG(CASE WHEN outcome='WIN' THEN 1.0 ELSE 0 END) FILTER (WHERE source='live_v2') AS wr_live,
              AVG(CASE WHEN outcome='WIN' THEN 1.0 ELSE 0 END) FILTER (WHERE source='hf_backfill') AS wr_hf,
              COUNT(*) FILTER (WHERE source='hf_backfill') AS n_hf
            FROM bucketed
            """,
            det,
            cat,
            zone,
        )
        wr_live = float(hf_row["wr_live"] or 0) if hf_row else 0
        wr_hf = float(hf_row["wr_hf"] or 0) if hf_row else 0
        n_hf = int(hf_row["n_hf"] or 0) if hf_row else 0
        delta_wr = round(abs(wr_live - wr_hf) * 100, 1) if n_hf > 0 else None
        criteria["hf_stability_delta_wr_pp"] = {
            "value": delta_wr,
            "threshold": 10,
            "pass": delta_wr is not None and delta_wr <= 10,
        }
        cap_row = await fetchrow(
            """
            SELECT PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY eo.book_depth_usd) AS median_depth,
                   PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY eo.expected_fill_rate) AS median_fill
              FROM execution_orders eo LEFT JOIN markets m ON m.polymarket_id = eo.market_id
             WHERE eo.detector = $1 AND COALESCE(m.category,'unknown') = $2
               AND CASE WHEN eo.signal_price < 0.40 THEN 'lt40'
                        WHEN eo.signal_price < 0.60 THEN '40-60'
                        WHEN eo.signal_price < 0.80 THEN '60-80'
                        WHEN eo.signal_price < 0.95 THEN '80-95'
                        ELSE 'gte95_blocked' END = $3
               AND eo.expected_fill_rate IS NOT NULL
               AND eo.created_at > now() - INTERVAL '7 days'
            """,
            det,
            cat,
            zone,
        )
        median_depth = float(cap_row["median_depth"] or 0) if cap_row else 0
        median_fill = float(cap_row["median_fill"] or 0) if cap_row else 0
        capacity_ratio = round(median_depth / 5.0, 2) if median_depth else None
        criteria["capacity_ratio"] = {
            "value": capacity_ratio,
            "threshold": 5.0,
            "pass": capacity_ratio is not None and capacity_ratio >= 5.0 and median_fill >= 0.95,
            "median_fill_rate": round(median_fill, 3),
        }
        settle_row = await fetchrow(
            """
            SELECT COUNT(*) AS n FROM execution_orders eo
              JOIN detector_signals ds ON ds.id = eo.detector_signal_id
             WHERE eo.status IN ('filled','virtual_filled') AND ds.resolved=true
               AND ds.outcome IN ('WIN','LOSS')
            """
        )
        n_unsettled = int(settle_row["n"]) if settle_row else 0
        criteria["settlement_e2e"] = {"value": n_unsettled, "threshold": 0, "pass": n_unsettled == 0}
        all_pass = all(c["pass"] for c in criteria.values())
        results.append(
            {
                "bucket": f"{det}/{cat}/{zone}",
                "detector": det,
                "category": cat,
                "entry_zone": zone,
                "criteria": criteria,
                "verdict": "ready" if all_pass else "not_ready",
            }
        )
    summary = {
        "total_buckets": len(results),
        "ready_buckets": sum(1 for r in results if r["verdict"] == "ready"),
        "next_actions": [],
    }
    if summary["ready_buckets"] == 0:
        summary["next_actions"].append("Подождать 24-48h после PR1 deploy.")
    for r in results:
        if r["verdict"] == "not_ready":
            fails = [k for k, v in r["criteria"].items() if not v["pass"]]
            summary["next_actions"].append(f"{r['bucket']}: {', '.join(fails)}")
    return {"summary": summary, "buckets": results}


# =============================================================================
# Auto Edge Scanner — research discoveries dashboard (PR #423)
# =============================================================================


@router.get("/v2/auto-edge/discoveries")
async def bot_v2_auto_edge_discoveries(
    _admin: dict = Depends(check_admin),
    status: str | None = Query(None, description="Filter: novel|subset|partial_overlap|promoted|dismissed"),
    limit: int = Query(50, ge=1, le=500),
):
    """Auto edge scanner findings — latest scan first, sorted by rank_score DESC.

    Showcases candidates discovered by weekly Sun 03:00 UTC grid sweep.
    Manual promote → INSERT into detector_state + create docs/backtests/Dx.md.
    """
    where_clauses = ["1=1"]
    params: list = []
    if status:
        statuses = [s.strip() for s in status.split(",") if s.strip()]
        if statuses:
            placeholders = ",".join(f"${i + 1}" for i in range(len(statuses)))
            where_clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)

    where_sql = " AND ".join(where_clauses)
    rows = await fetch(
        f"""
        SELECT
            id, scan_run_id, scan_date,
            feature, threshold_op, threshold_value,
            zone, zone_lo, zone_hi, anchor_hours,
            category, side,
            n_signals, n_unique_markets,
            win_rate, avg_cost,
            net_roi, mirror_net_roi, mirror_diff_pp,
            year_2025_net_roi, year_2026_net_roi, year_consistency_score,
            daily_signals, daily_raw_pnl_at_5k, daily_honest_pnl_at_5k,
            p_value, bh_adjusted_p, rank_score,
            overlap_pct_max,
            status, verdict,
            cell_signature, first_seen_scan_date, n_times_seen,
            notified_at, promoted_to, notes,
            created_at
          FROM auto_edge_discoveries
         WHERE {where_sql}
         ORDER BY scan_date DESC, rank_score DESC
         LIMIT ${len(params) + 1}
        """,
        *params,
        limit,
    )
    return {"discoveries": records_to_list(rows)}


@router.get("/v2/auto-edge/scans")
async def bot_v2_auto_edge_scans(_admin: dict = Depends(check_admin), limit: int = Query(20, ge=1, le=100)):
    """Recent scan summary: per-run aggregates."""
    rows = await fetch(
        """
        SELECT
            scan_run_id,
            scan_date,
            COUNT(*) AS n_findings,
            COUNT(*) FILTER (WHERE status = 'novel') AS n_novel,
            COUNT(*) FILTER (WHERE status = 'subset') AS n_subset,
            COUNT(*) FILTER (WHERE status = 'partial_overlap') AS n_partial,
            COUNT(*) FILTER (WHERE status = 'promoted') AS n_promoted,
            COUNT(*) FILTER (WHERE notified_at IS NOT NULL) AS n_notified,
            MAX(rank_score) AS max_rank_score,
            MIN(created_at) AS first_seen
          FROM auto_edge_discoveries
         GROUP BY scan_run_id, scan_date
         ORDER BY scan_date DESC, first_seen DESC
         LIMIT $1
        """,
        limit,
    )
    return {"scans": records_to_list(rows)}


@router.post("/v2/auto-edge/discoveries/{discovery_id}/dismiss")
@limiter.limit("60/minute")
async def bot_v2_auto_edge_dismiss(
    request: Request,
    discovery_id: int,
    _admin: dict = Depends(check_admin),
):
    """Mark discovery as dismissed (out of consideration)."""
    row = await fetchrow(
        """
        UPDATE auto_edge_discoveries
           SET status = 'dismissed', updated_at = now()
         WHERE id = $1
        RETURNING id, status, cell_signature
        """,
        discovery_id,
    )
    if row is None:
        return {"ok": False, "error": "not_found"}
    return {"ok": True, "discovery": dict(row)}


@router.post("/v2/auto-edge/discoveries/{discovery_id}/promote")
@limiter.limit("60/minute")
async def bot_v2_auto_edge_promote(
    request: Request,
    discovery_id: int,
    _admin: dict = Depends(check_admin),
    detector_name: str = Query(..., description="Target detector slug, e.g. D15_smart_money_taker_high"),
):
    """Mark discovery as promoted (link to detector). Detector creation is manual."""
    row = await fetchrow(
        """
        UPDATE auto_edge_discoveries
           SET status = 'promoted', promoted_to = $2, updated_at = now()
         WHERE id = $1
        RETURNING id, status, promoted_to, cell_signature
        """,
        discovery_id,
        detector_name,
    )
    if row is None:
        return {"ok": False, "error": "not_found"}
    return {"ok": True, "discovery": dict(row)}


@router.get("/v2/kill-switch")
async def bot_v2_kill_switch_status(_admin: dict = Depends(check_admin)) -> dict:
    """Реальное состояние bot:kill_switch (audit топ-6: дашборд врал disarmed)."""
    redis = await get_redis()
    value = await redis.get("bot:kill_switch") if redis else None
    return {"tripped": bool(value), "reason": value or None}


@router.delete("/v2/kill-switch")
async def bot_v2_kill_switch_clear(_admin: dict = Depends(check_admin)) -> dict:
    redis = await get_redis()
    if redis is None:
        raise HTTPException(status_code=503, detail="redis unavailable")
    value = await redis.get("bot:kill_switch")
    await redis.delete("bot:kill_switch")
    logger.warning("kill_switch_manual_clear prev_reason=%s", value)
    await send_admin_message(f"🔓 Kill switch снят вручную (API). Была причина: {value or '—'}")
    return {"cleared": True, "prev_reason": value}


@router.post("/v2/live/close/{order_id}")
@limiter.limit("60/minute")
async def bot_v2_live_close_position(
    request: Request,
    order_id: str,
    _admin: dict = Depends(check_admin),
):
    """Аварийный ручной close live-позиции (audit топ-1: live был BUY-only, SELL не было).

    Тот же сервис, что и TG-кнопка "🔴 Закрыть позицию" (см. app/api/telegram.py
    close_pos: callback), только через REST для непрямого доступа.
    """
    from app.services.live_exit import close_live_position

    ok, msg = await close_live_position(order_id, reason="manual_api")
    if not ok:
        return {"ok": False, "error": msg}
    return {"ok": True, "exit_order_id": msg}
