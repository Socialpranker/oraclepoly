"""Tier-2 Insights API.

Чтение pre-computed aggregates из 5 materialized views для React dashboard.
Все endpoints возвращают ready-to-render JSON, без heavy computation.

См. migrations/20260521090451_create_tier2_materialized_views.sql.
"""

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Query

from app.core.auth import check_admin
from app.services.database import fetch, fetchrow, records_to_list

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/uma-disputes")
async def uma_disputes(
    _admin: dict = Depends(check_admin),
    limit: int = Query(50, ge=1, le=500),
    min_disputes: int = Query(0, ge=0),
) -> dict[str, Any]:
    """UMA dispute timeline — markets с историей disputes.

    Sort by dispute_count desc, потом по updated_at desc (свежие сверху).
    """
    rows = await fetch(
        """
        SELECT
            market_id::text,
            condition_id,
            question,
            category,
            current_status,
            dispute_count,
            proposed_count,
            status_changes,
            uma_bond,
            uma_reward,
            current_price,
            resolved,
            winner,
            end_date,
            updated_at
        FROM mv_uma_dispute_timeline
        WHERE dispute_count >= $2
        ORDER BY
            CASE WHEN current_status IN ('proposed', 'disputed') THEN 0 ELSE 1 END,
            dispute_count DESC,
            updated_at DESC
        LIMIT $1
        """,
        limit,
        min_disputes,
    )
    return {"items": records_to_list(rows), "count": len(rows)}


@router.get("/manifold-divergence")
async def manifold_divergence(
    _admin: dict = Depends(check_admin),
    limit: int = Query(50, ge=1, le=500),
    min_divergence_pp: float = Query(0.05, ge=0.0, le=1.0),
) -> dict[str, Any]:
    """Polymarket ↔ Manifold divergence — pairs с |poly−manifold| >= threshold.

    Sort by abs_divergence_pp desc. Default threshold 5pp (D46 trigger).
    """
    rows = await fetch(
        """
        SELECT
            canonical_event_id,
            polymarket_market_id::text,
            polymarket_condition_id,
            polymarket_question,
            polymarket_yes_prob,
            polymarket_volume_24h,
            manifold_market_id,
            manifold_question,
            manifold_yes_prob,
            manifold_volume_24h,
            divergence_pp,
            abs_divergence_pp,
            last_manifold_bet,
            manifold_bets_7d,
            polymarket_updated_at,
            manifold_updated_at
        FROM mv_manifold_polymarket_pairs
        WHERE abs_divergence_pp >= $2
        ORDER BY abs_divergence_pp DESC
        LIMIT $1
        """,
        limit,
        min_divergence_pp,
    )
    return {"items": records_to_list(rows), "count": len(rows)}


@router.get("/whale-shocks")
async def whale_shocks(
    _admin: dict = Depends(check_admin),
    limit: int = Query(50, ge=1, le=500),
    trigger_type: str | None = Query(None),
) -> dict[str, Any]:
    """Whale shock recovery events — orderbook reaction после whale trade ≥$50k.

    Включает price impact, spread evolution, depth recovery ratio.
    """
    sql = """
        SELECT
            event_id::text,
            trigger_type,
            market_id::text,
            condition_id,
            question,
            category,
            event_start,
            event_end,
            window_span,
            n_snapshots,
            depth_at_start,
            depth_at_50s,
            depth_recovery_ratio,
            midpoint_at_start,
            midpoint_at_50s,
            price_impact_50s,
            spread_at_start,
            spread_at_50s,
            trigger_meta
        FROM mv_whale_shock_recovery
    """
    params: list = []
    if trigger_type:
        sql += " WHERE trigger_type = $1"
        params.append(trigger_type)
    sql += f" ORDER BY event_start DESC LIMIT ${len(params) + 1}"
    params.append(limit)

    rows = await fetch(sql, *params)
    return {"items": records_to_list(rows), "count": len(rows)}


@router.get("/term-structure")
async def term_structure(
    _admin: dict = Depends(check_admin),
    limit: int = Query(100, ge=1, le=1000),
    regime: str | None = Query(None),
) -> dict[str, Any]:
    """Markets × term-structure momentum × outcome.

    Можно фильтровать по momentum_regime_1h: strong_up_1h, mild_up_1h, flat,
    mild_down_1h, strong_down_1h.
    """
    sql = """
        SELECT
            market_id::text,
            condition_id,
            category,
            question,
            yes_prob_current,
            one_hour_price_change,
            one_day_price_change,
            one_week_price_change,
            one_month_price_change,
            one_year_price_change,
            volume_24h,
            momentum_regime_1h,
            resolved,
            winner,
            outcome_signed,
            end_date,
            updated_at
        FROM mv_term_structure_momentum
    """
    params: list = []
    if regime:
        sql += " WHERE momentum_regime_1h = $1"
        params.append(regime)
    sql += f" ORDER BY abs(one_hour_price_change) DESC NULLS LAST LIMIT ${len(params) + 1}"
    params.append(limit)

    rows = await fetch(sql, *params)
    return {"items": records_to_list(rows), "count": len(rows)}


@router.get("/cohort-stats")
async def cohort_stats(_admin: dict = Depends(check_admin)) -> dict[str, Any]:
    """Per-cohort aggregated win-rates (category × cyom × restricted × neg_risk_aug).

    Возвращает все cohort'ы. UI отображает grouped by category.
    """
    rows = await fetch(
        """
        SELECT
            category,
            is_cyom,
            is_restricted,
            is_neg_risk_aug,
            n_markets,
            n_resolved,
            n_yes_won,
            n_no_won,
            yes_win_rate,
            avg_current_price,
            median_current_price,
            avg_volume_24h,
            total_volume_24h
        FROM mv_cohort_stats
        ORDER BY total_volume_24h DESC NULLS LAST
        """
    )
    return {"items": records_to_list(rows), "count": len(rows)}


@router.get("/action-feed")
async def action_feed(
    _admin: dict = Depends(check_admin),
    limit: int = Query(12, ge=1, le=50),
) -> dict[str, Any]:
    """Hybrid hunting feed — top actionable signals across all tier-2 channels.

    Объединяет 4 источника в один ranked list:
      - UMA: fresh disputes (status disputed/proposed, updated < 24h)
      - Manifold: high divergence (≥10pp) + recent MF bet (<48h)
      - Whale: poor recovery shock (ratio<0.7) с большим price impact (last 24h)
      - Term-structure: extreme 1h breakouts (|change|≥10pp + volume_24h>$10k)

    Каждая запись — ready-to-render: source, severity, headline, metric, market.
    Сортировка: severity DESC, recency DESC. Top N.
    """
    rows = await fetch(
        """
        WITH uma_signals AS (
            SELECT
                'uma'::text                   AS source,
                CASE
                    WHEN dispute_count >= 2 THEN 'high'
                    WHEN current_status = 'disputed' THEN 'high'
                    WHEN current_status = 'proposed' THEN 'med'
                    ELSE 'low'
                END                            AS severity,
                CASE
                    WHEN dispute_count >= 2 THEN 'Multi-disputed (' || dispute_count || ')'
                    WHEN current_status = 'disputed' THEN 'Active dispute'
                    WHEN current_status = 'proposed' THEN 'Pending proposal'
                    ELSE 'UMA flag'
                END                            AS headline,
                question                       AS market_question,
                condition_id,
                category,
                jsonb_build_object(
                    'status', current_status,
                    'dispute_count', dispute_count,
                    'bond', uma_bond,
                    'current_price', current_price
                )                              AS payload,
                updated_at                     AS event_at
            FROM mv_uma_dispute_timeline
            WHERE current_status IN ('proposed', 'disputed')
               OR (dispute_count >= 2 AND NOT resolved)
        ),
        manifold_signals AS (
            SELECT
                'manifold'::text               AS source,
                CASE
                    WHEN abs_divergence_pp >= 0.15 AND manifold_bets_7d > 0 THEN 'high'
                    WHEN abs_divergence_pp >= 0.10 THEN 'med'
                    ELSE 'low'
                END                            AS severity,
                CASE
                    WHEN divergence_pp > 0 THEN '+' || round((divergence_pp * 100)::numeric, 1) || 'pp Poly > MF'
                    ELSE round((divergence_pp * 100)::numeric, 1) || 'pp MF > Poly'
                END                            AS headline,
                polymarket_question            AS market_question,
                polymarket_condition_id        AS condition_id,
                NULL::text                     AS category,
                jsonb_build_object(
                    'poly_yes', polymarket_yes_prob,
                    'mf_yes', manifold_yes_prob,
                    'divergence_pp', divergence_pp,
                    'mf_bets_7d', manifold_bets_7d,
                    'last_mf_bet', last_manifold_bet
                )                              AS payload,
                COALESCE(last_manifold_bet, polymarket_updated_at) AS event_at
            FROM mv_manifold_polymarket_pairs
            WHERE abs_divergence_pp >= 0.10
        ),
        whale_signals AS (
            SELECT
                'whale'::text                  AS source,
                CASE
                    WHEN depth_recovery_ratio < 0.5 AND abs(price_impact_50s) >= 0.02 THEN 'high'
                    WHEN depth_recovery_ratio < 0.7 OR abs(price_impact_50s) >= 0.01 THEN 'med'
                    ELSE 'low'
                END                            AS severity,
                CASE
                    WHEN depth_recovery_ratio IS NULL THEN 'Shock (depth n/a)'
                    WHEN depth_recovery_ratio < 0.7
                        THEN 'Fragile depth ' || round((depth_recovery_ratio * 100)::numeric, 0) || '%'
                    ELSE 'Recovered ' || round((depth_recovery_ratio * 100)::numeric, 0) || '%'
                END                            AS headline,
                question                       AS market_question,
                condition_id,
                category,
                jsonb_build_object(
                    'trigger_type', trigger_type,
                    'recovery_ratio', depth_recovery_ratio,
                    'price_impact_50s', price_impact_50s,
                    'depth_at_start', depth_at_start
                )                              AS payload,
                event_start                    AS event_at
            FROM mv_whale_shock_recovery
            WHERE event_start > now() - interval '24 hours'
        ),
        term_signals AS (
            SELECT
                'term'::text                   AS source,
                CASE
                    WHEN abs(one_hour_price_change) >= 0.15 AND volume_24h >= 10000 THEN 'high'
                    WHEN abs(one_hour_price_change) >= 0.10 THEN 'med'
                    ELSE 'low'
                END                            AS severity,
                CASE
                    WHEN one_hour_price_change > 0
                        THEN '+' || round((one_hour_price_change * 100)::numeric, 1) || 'pp 1h'
                    ELSE round((one_hour_price_change * 100)::numeric, 1) || 'pp 1h'
                END                            AS headline,
                question                       AS market_question,
                condition_id,
                category,
                jsonb_build_object(
                    'one_hour_change', one_hour_price_change,
                    'one_day_change', one_day_price_change,
                    'volume_24h', volume_24h,
                    'yes_prob', yes_prob_current,
                    'regime', momentum_regime_1h
                )                              AS payload,
                updated_at                     AS event_at
            FROM mv_term_structure_momentum
            WHERE NOT resolved
              AND abs(one_hour_price_change) >= 0.10
              AND volume_24h >= 5000
        ),
        unioned AS (
            SELECT * FROM uma_signals
            UNION ALL
            SELECT * FROM manifold_signals
            UNION ALL
            SELECT * FROM whale_signals
            UNION ALL
            SELECT * FROM term_signals
        )
        SELECT *
        FROM unioned
        ORDER BY
            CASE severity WHEN 'high' THEN 0 WHEN 'med' THEN 1 ELSE 2 END,
            event_at DESC NULLS LAST
        LIMIT $1
        """,
        limit,
    )

    # Freshness probe — MV refresh time
    freshness = await fetch(
        """
        SELECT
            (SELECT max(updated_at) FROM mv_uma_dispute_timeline)         AS uma_last,
            (SELECT max(polymarket_updated_at) FROM mv_manifold_polymarket_pairs) AS manifold_last,
            (SELECT max(event_start) FROM mv_whale_shock_recovery)        AS whale_last,
            (SELECT max(updated_at) FROM mv_term_structure_momentum)      AS term_last
        """
    )

    # asyncpg отдаёт jsonb-колонку (payload) строкой — распаковываем в объект,
    # т.к. фронт (ActionItem.payload) типизирован как Record<string, unknown>.
    # Тот же приём для jsonb см. cohort_stats ниже (meta).
    items = records_to_list(rows)
    for item in items:
        payload = item.get("payload")
        if isinstance(payload, str):
            item["payload"] = json.loads(payload or "{}")

    return {
        "items": items,
        "count": len(rows),
        "freshness": records_to_list(freshness)[0] if freshness else {},
    }


@router.get("/summary")
async def summary(_admin: dict = Depends(check_admin)) -> dict[str, Any]:
    """Aggregate counters для top-level dashboard cards.

    Один запрос на dashboard load — суммарные числа по каждому каналу.
    """
    uma_summary = await fetch(
        """
        SELECT
            count(*) FILTER (WHERE current_status IN ('proposed', 'disputed')) AS active_disputes,
            count(*) FILTER (WHERE dispute_count >= 2) AS multi_disputed,
            count(*) AS total_with_uma
        FROM mv_uma_dispute_timeline
        """
    )

    manifold_summary = await fetch(
        """
        SELECT
            count(*) AS pairs_total,
            count(*) FILTER (WHERE abs_divergence_pp >= 0.10) AS high_divergence_count,
            count(*) FILTER (WHERE manifold_bets_7d > 0) AS active_manifold_pairs
        FROM mv_manifold_polymarket_pairs
        """
    )

    whale_summary = await fetch(
        """
        SELECT
            count(*) AS events_total,
            count(*) FILTER (WHERE event_start > now() - interval '24 hours') AS events_24h,
            avg(depth_recovery_ratio) FILTER (WHERE depth_recovery_ratio IS NOT NULL) AS avg_recovery_ratio,
            percentile_cont(0.95) WITHIN GROUP (ORDER BY abs(price_impact_50s)) FILTER (WHERE price_impact_50s IS NOT NULL) AS p95_price_impact
        FROM mv_whale_shock_recovery
        """
    )

    return {
        "uma": records_to_list(uma_summary)[0] if uma_summary else {},
        "manifold": records_to_list(manifold_summary)[0] if manifold_summary else {},
        "whale_shocks": records_to_list(whale_summary)[0] if whale_summary else {},
    }


@router.get("/ml-accuracy")
async def ml_accuracy(
    _admin: dict = Depends(check_admin),
) -> dict[str, Any]:
    """ML accuracy панель (Diagnostics → ML Accuracy).

    Читает последний снимок из ml_accuracy_snapshots (пишется weekly cron
    ml-accuracy-snapshot). 4 слоя: honesty (Brier/calib per anchor), selection
    (куда ставить), timing (когда), money (ROI per category с флагом RELIABLE/
    ARTIFACT). Возвращает ready-to-render JSON, без heavy computation.

    Honest cohort (дедуп + event-health + cutoff) применяется на стороне движка —
    см. scripts/research/ml_accuracy_report.py.
    """
    # Статус активности ML-моделей (L2 / Layer A / Layer B) — чтобы 0-active
    # читалось как frozen-by-gate, а не как пустота/поломка. Latest row каждой
    # таблицы: is_active + fit_at + last_skip_reason (причина НЕ-промоута).
    model_status = await _fetch_model_status()

    rows = await fetch(
        """
        SELECT layer, detector, scope, metric_key, metric_value,
               cohort_n, n_events, reliability, meta
        FROM ml_accuracy_snapshots
        WHERE snapshot_at = (SELECT MAX(snapshot_at) FROM ml_accuracy_snapshots)
        ORDER BY layer, detector, scope, metric_key
        """
    )
    if not rows:
        return {"layers": {}, "money": [], "freshness": None, "model_status": model_status}

    # group honesty/selection/timing by (detector, scope) → {metric_key: value}
    layers: dict[str, dict[str, dict[str, Any]]] = {"honesty": {}, "selection": {}, "timing": {}}
    money: list[dict[str, Any]] = []
    for r in rows:
        layer = r["layer"]
        if layer == "money":
            meta = r["meta"] if isinstance(r["meta"], dict) else json.loads(r["meta"] or "{}")
            money.append(
                {
                    "detector": r["detector"],
                    "category": r["scope"],
                    "avg_roi_net_pct": r["metric_value"],
                    "n_signals": r["cohort_n"],
                    "n_events": r["n_events"],
                    "reliability": r["reliability"],
                    **meta,
                }
            )
            continue
        key = f"{r['detector']}|{r['scope']}"
        bucket = layers[layer].setdefault(
            key,
            {
                "detector": r["detector"],
                "scope": r["scope"],
                "cohort_n": r["cohort_n"],
                "n_events": r["n_events"],
                "metrics": {},
            },
        )
        bucket["metrics"][r["metric_key"]] = r["metric_value"]

    # freshness — отдельным запросом получаем snapshot_at (не в SELECT выше)
    fresh = await fetch("SELECT MAX(snapshot_at) AS at FROM ml_accuracy_snapshots")
    return {
        "layers": {k: list(v.values()) for k, v in layers.items()},
        "money": money,
        "freshness": {"snapshot_at": fresh[0]["at"].isoformat() if fresh and fresh[0]["at"] else None},
        "model_status": model_status,
    }


async def _fetch_model_status() -> list[dict[str, Any]]:
    """Статус активности L2 / Layer A / Layer B для дашборда.

    Для каждой модели берём latest row (по fit_at): активна ли, когда fit, и
    last_skip_reason если НЕ промоутнута. UI выводит ACTIVE / FROZEN (gate) badge,
    чтобы 0-active не выглядело как тихая поломка. См. strategy_edge_not_data_2026-06-15.
    """
    specs = [
        ("L2 meta-labeler", "ml_meta_labeler"),
        ("Layer A event-selector", "ml_event_selector"),
        ("Layer B market-picker", "ml_market_picker"),
    ]
    out: list[dict[str, Any]] = []
    for label, table in specs:
        row = await fetchrow(
            f"SELECT version, is_active, fit_at, last_skip_reason "  # noqa: S608
            f"FROM {table} ORDER BY fit_at DESC LIMIT 1"
        )
        if row is None:
            out.append({"label": label, "state": "никогда не обучалась", "active": False})
            continue
        out.append(
            {
                "label": label,
                "active": bool(row["is_active"]),
                "version": row["version"],
                "fit_at": row["fit_at"].isoformat() if row["fit_at"] else None,
                "skip_reason": row["last_skip_reason"],
            }
        )
    return out
