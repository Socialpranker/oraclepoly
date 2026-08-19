"""Market calibration API — честная калибровка рынка на нейтральной выборке.

Читает mv_market_calibration (daily_price_snapshots ⋈ markets.winner).
calib_gap = actual_winrate - implied_prob (отриц = YES переоценён).
Edge реален только при n>=40 И Wilson CI не пересекает implied_prob.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends

from app.core.auth import check_admin
from app.services.database import fetch, records_to_list

router = APIRouter()


@router.get("/curve")
async def calibration_curve(
    horizon: Literal["near", "mid", "far", "deep"] = "far",
    _admin: dict = Depends(check_admin),
) -> dict[str, Any]:
    """Reliability curve по price_bucket для заданного horizon (near/mid/far/deep).

    Агрегирует по price_bucket (все категории вместе), чтобы получить классическую
    калибровочную кривую implied vs actual.
    """
    rows = await fetch(
        """
        SELECT
            price_bucket,
            SUM(n)                                          AS n,
            ROUND(SUM(implied_prob * n) / NULLIF(SUM(n), 0), 4)   AS implied_prob,
            ROUND(SUM(actual_winrate * n) / NULLIF(SUM(n), 0), 4) AS actual_winrate,
            ROUND(SUM((actual_winrate - implied_prob) * n) / NULLIF(SUM(n), 0), 4) AS calib_gap
        FROM mv_market_calibration
        WHERE horizon_bucket = $1
        GROUP BY price_bucket
        ORDER BY price_bucket
        """,
        horizon,
    )
    return {"horizon": horizon, "items": records_to_list(rows), "count": len(rows)}


@router.get("/edges")
async def calibration_edges(_admin: dict = Depends(check_admin)) -> dict[str, Any]:
    """Ячейки со статистически значимым калибровочным перекосом (потенциальный edge).

    Значимость: n>=40 И Wilson CI на actual_winrate НЕ пересекает implied_prob.
    Это кандидаты на detector-идеи. Отсортированы по |calib_gap|.
    """
    rows = await fetch(
        """
        SELECT
            horizon_bucket, price_bucket, category, n,
            implied_prob, actual_winrate, calib_gap, brier, wilson_lower, wilson_upper,
            CASE WHEN wilson_upper < implied_prob THEN 'yes_overpriced'
                 WHEN wilson_lower > implied_prob THEN 'yes_underpriced'
            END AS direction
        FROM mv_market_calibration
        WHERE n >= 40
          AND (wilson_upper < implied_prob OR wilson_lower > implied_prob)
        ORDER BY ABS(calib_gap) DESC
        """
    )
    return {"items": records_to_list(rows), "count": len(rows)}


@router.get("/by-category")
async def calibration_by_category(_admin: dict = Depends(check_admin)) -> dict[str, Any]:
    """Калибровка, свёрнутая по категории (across price/horizon), для обзора.

    Volume-взвешенный gap + общий brier на категорию. n>=40 на категорию.
    """
    # ORDER BY оборачивает алиас weighted_gap в подзапросе — Postgres не
    # допускает ABS(<select-alias>) в ORDER BY самого SELECT'а, только bare-алиас.
    rows = await fetch(
        """
        SELECT category, n, avg_implied, avg_actual, weighted_gap, weighted_brier
        FROM (
            SELECT
                category,
                SUM(n)                                          AS n,
                ROUND(SUM(implied_prob * n) / NULLIF(SUM(n), 0), 4)   AS avg_implied,
                ROUND(SUM(actual_winrate * n) / NULLIF(SUM(n), 0), 4) AS avg_actual,
                ROUND(SUM((actual_winrate - implied_prob) * n) / NULLIF(SUM(n), 0), 4) AS weighted_gap,
                ROUND(SUM(brier * n) / NULLIF(SUM(n), 0), 4)          AS weighted_brier
            FROM mv_market_calibration
            GROUP BY category
            HAVING SUM(n) >= 40
        ) c
        ORDER BY ABS(weighted_gap) DESC
        """
    )
    return {"items": records_to_list(rows), "count": len(rows)}
