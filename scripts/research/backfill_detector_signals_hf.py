"""Unified backfill detector_signals из HF canonical layer.

Цель: восстановить detector_signals для backfill-friendly детекторов через
канонический dataset (markets_clean + price_snapshots + trade_buckets) чтобы
у ML моделей был достаточный training set после migration 2026-05-11.

Архитектура:
1. Iterate по price_snapshots (~1.48M rows, anchors 4/8/12/24/48/72/168/720h)
2. Для каждого snapshot:
   - JOIN markets_clean для category/horizon_days/yes_won/neg_risk
   - JOIN trade_buckets (volume_24h proxy via аналог trailing 24h)
   - Construct market_event dict
   - Вызвать detector.detect()
   - Если Signal — resolve outcome по markets_clean.yes_won
   - INSERT в detector_signals(source='hf_backfill', resolved=true)

Поддерживаемые детекторы (backfill-friendly):
    D13_retail_confidence_fade     single-market (+ smart_money, n_takers)
    D14_crypto_winners_distribute  single-market (+ wallet_quality)
    D3_cross_mkt_arb               event-level (multi-market)
    D29_negrisk_basket_arb         event-level (basket)

Запуск ТОЛЬКО на проде (canonical /mnt/data/canonical/, 1.5 GB):
    docker exec oraclepoly-backend python scripts/research/backfill_detector_signals.py \\
        --detector D14 --dry-run

    docker exec oraclepoly-backend python scripts/research/backfill_detector_signals.py \\
        --detector all

Idempotency: ON CONFLICT DO NOTHING через synthetic tx_hash в evidence
(`backfill_{detector}_{market_id}_{anchor_h}h`).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Allow execution from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

# duckdb импортируется лениво в backfill_* функциях — он есть только в Docker на проде.
# Локально (для pytest) модуль импортируется чтобы тестировать pure helpers/adapters
# без необходимости устанавливать duckdb в .venv.

from app.detectors import (
    d3_cross_mkt_arb,
    d13_retail_confidence_fade,
    d14_crypto_winners_distribute_no,
    d29_negrisk_basket_arb,
)
from app.detectors.contract import Signal

# asyncpg импортируется лениво (нужен только при не-dry-run). Локально для
# тестов pure helpers/adapters достаточно — asyncpg может отсутствовать.

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill")


# ── Realized ROI / fee / slippage formulas ────────────────────────────────
# Inline-копия app.services.fee_calculator + app.core.slippage + app.jobs.
# detector_signals_resolve чтобы не тянуть pydantic_settings + asyncpg lifecycle.

PAPER_STAKE_USD = 1.0

# Polymarket fee schedule (source: app/services/fee_calculator.py — SSoT)
_FEE_SCHEDULE: dict[str, tuple[float, float]] = {
    "crypto": (0.072, 1),
    "sports": (0.030, 1),
    "finance": (0.040, 1),
    "politics": (0.040, 1),
    "economics": (0.050, 1),
    "culture": (0.050, 1),
    "weather": (0.050, 1),
    "tech": (0.040, 1),
    "mentions": (0.040, 1),
    "other": (0.050, 1),
    "geopolitics": (0.0, 1),
}
_CATEGORY_MAP: dict[str, str] = {"tech & ai": "tech", "world & economy": "geopolitics"}
_DEFAULT_FEE: tuple[float, float] = (0.050, 1)

# Half-spread per category (source: app/core/slippage.py — SSoT)
_HALF_SPREAD: dict[str, float] = {
    "sports": 0.020,
    "politics": 0.025,
    "crypto": 0.030,
    "culture": 0.030,
    "finance": 0.025,
    "tech & ai": 0.030,
    "weather": 0.035,
    "world & economy": 0.030,
    "geopolitics": 0.030,
}
_DEFAULT_SPREAD = 0.030


def _estimate_trading_fee(amount: float, price: float, category: str) -> float:
    cat = (category or "other").lower().strip()
    key = _CATEGORY_MAP.get(cat, cat)
    rate, k = _FEE_SCHEDULE.get(key, _DEFAULT_FEE)
    if rate == 0.0:
        return 0.0
    return amount * rate * (price * (1.0 - price)) ** k


def _get_spread_estimate(category: str) -> float:
    return _HALF_SPREAD.get((category or "").lower(), _DEFAULT_SPREAD)


def _entry_price_for_side(current_price: float, side: str) -> float:
    if side == "YES":
        return current_price
    return 1.0 - current_price


def _realized_roi_pct(outcome: str, current_price: float, side: str) -> float:
    """Gross ROI на $1 paper-stake, side-aware. Matches app.jobs.detector_signals_resolve."""
    if outcome == "WIN":
        entry = _entry_price_for_side(current_price, side)
        if entry <= 0:
            return 0.0
        return round((1.0 / entry - 1.0) * 100.0, 3)
    if outcome == "LOSS":
        return -100.0
    return 0.0


def _realized_roi_net_pct(outcome: str, current_price: float, side: str, category: str | None) -> float:
    """Net ROI per-notional с fee + half-spread. Matches app.jobs.detector_signals_resolve."""
    if outcome not in ("WIN", "LOSS"):
        return 0.0
    entry = _entry_price_for_side(current_price, side)
    if entry <= 0:
        return 0.0
    cat = (category or "other").lower()
    eff = max(entry, 0.001)
    payout = 1.0 / eff - 1.0
    notional = PAPER_STAKE_USD / eff
    gross_notional = PAPER_STAKE_USD * (1.0 + payout)
    fee_one_leg = _estimate_trading_fee(gross_notional, eff, cat)
    slippage = PAPER_STAKE_USD * _get_spread_estimate(cat)
    if outcome == "WIN":
        net_pnl = PAPER_STAKE_USD * payout - 2.0 * fee_one_leg - slippage
    else:
        net_pnl = -(PAPER_STAKE_USD + fee_one_leg + slippage)
    return round(net_pnl / notional * 100.0, 3)


# ── asyncpg pool (нужен только для не-dry-run) ────────────────────────────
_DB_POOL = None  # type: ignore[var-annotated]


async def _db_init() -> None:
    """Создать asyncpg pool. DSN читается из env DATABASE_URL / DB_DSN."""
    global _DB_POOL
    import os

    import asyncpg  # lazy import

    dsn = os.environ.get("DATABASE_URL") or os.environ.get("DB_DSN")
    if not dsn:
        raise RuntimeError("DATABASE_URL / DB_DSN env var required для не-dry-run mode")
    _DB_POOL = await asyncpg.create_pool(dsn, min_size=1, max_size=4)


async def _db_close() -> None:
    if _DB_POOL is not None:
        await _DB_POOL.close()


async def _db_execute(sql: str, *params: Any) -> None:
    assert _DB_POOL is not None
    async with _DB_POOL.acquire() as conn:
        await conn.execute(sql, *params)

# ── Paths ────────────────────────────────────────────────────────────────
CANONICAL_DIR = Path("/mnt/data/canonical")
MARKETS_PARQUET = CANONICAL_DIR / "markets_clean.parquet"
SNAPSHOTS_PARQUET = CANONICAL_DIR / "price_snapshots.parquet"
TRADE_BUCKETS_PARQUET = CANONICAL_DIR / "trade_buckets.parquet"
WALLET_FLOW_PARQUET = CANONICAL_DIR / "wallet_flow_per_market.parquet"
WALLET_SCORE_PARQUET = CANONICAL_DIR / "wallet_score_at_anchor.parquet"

# ── SQL ──────────────────────────────────────────────────────────────────
_INSERT_SQL = """
INSERT INTO detector_signals (
    detector, market_id, event_id, side,
    confidence, expected_edge_pct, fair_price, current_price,
    horizon_sec, token_id, evidence, generated_at,
    source, resolved, outcome, realized_roi, realized_roi_net, resolved_at
)
VALUES (
    $1, $2, $3, $4,
    $5, $6, $7, $8,
    $9, $10, $11::jsonb, $12,
    'hf_backfill', true, $13, $14, $15, $16
)
ON CONFLICT DO NOTHING
"""


# ── Single-market detector specs ─────────────────────────────────────────
@dataclass(frozen=True)
class SingleMarketSpec:
    """Описывает single-market детектор для backfill."""

    name: str  # short name: 'D14', 'D13', ...
    detector_name: str  # full name пишемый в detector_signals.detector
    detect_fn: Callable[[dict[str, Any]], Signal | None]
    # Anchors из price_snapshots которые имеет смысл прогнать (часы до резолва)
    anchors_h: tuple[int, ...]
    needs_question: bool = False  # D13
    needs_smart_money: bool = False  # D13 required
    needs_n_takers_24h_before: bool = False  # D13
    needs_wallet_quality: bool = False  # D14 (pnl/roi weighted + buy_imbalance)


SINGLE_MARKET_SPECS: dict[str, SingleMarketSpec] = {
    "D13": SingleMarketSpec(
        name="D13",
        detector_name=d13_retail_confidence_fade.DETECTOR_NAME,
        detect_fn=d13_retail_confidence_fade.detect,
        anchors_h=(4, 12, 24, 72),
        needs_question=True,
        needs_smart_money=True,  # REQUIRED gate < 0.05
        needs_n_takers_24h_before=True,  # REQUIRED >= 3
    ),
    "D14": SingleMarketSpec(
        name="D14",
        detector_name=d14_crypto_winners_distribute_no.DETECTOR_NAME,
        detect_fn=d14_crypto_winners_distribute_no.detect,
        anchors_h=(4,),  # only 4h band passes per canonical
        needs_wallet_quality=True,
    ),
}

# Event-level detectors — обрабатываются отдельной функцией
EVENT_LEVEL_DETECTORS: tuple[str, ...] = ("D3", "D29")

ALL_DETECTORS: tuple[str, ...] = tuple(SINGLE_MARKET_SPECS.keys()) + EVENT_LEVEL_DETECTORS


# ── DuckDB query builders ────────────────────────────────────────────────
def _build_snapshot_query(
    *,
    anchor_h: int,
    needs_question: bool,
    needs_smart_money: bool = False,
    needs_n_takers_24h: bool = False,
    needs_wallet_quality: bool = False,
    limit: int | None = None,
) -> str:
    """Строит SQL для single-market detector backfill.

    Returns rows: market_id, condition_id, event_id, category, event_title,
    question (если needs_question), current_price, neg_risk, horizon_days,
    lifetime_volume_usd, hours_to_resolve, yes_won, trade_ts,
    volume_24h (trailing 24h sum from trade_buckets),
    smart_money_taker_share (если needs_smart_money) — pre-anchor 24h window,
    n_takers_24h_before (если needs_n_takers_24h)
    """
    extra_cols = []
    if needs_question:
        extra_cols.append("m.question")

    extra_joins: list[str] = []

    # Smart money pre-anchor 24h window (D13)
    smart_money_cte = ""
    smart_money_select = ""
    smart_money_join = ""
    if needs_smart_money or needs_n_takers_24h:
        smart_money_cte = f""",
        wallet_flow_24h AS (
            SELECT condition_id,
                   hour_bucket,
                   SUM(whale_taker_volume) OVER (
                       PARTITION BY condition_id
                       ORDER BY hour_bucket
                       RANGE BETWEEN INTERVAL 24 HOUR PRECEDING AND CURRENT ROW
                   ) AS whale_vol_24h,
                   SUM(taker_volume_total) OVER (
                       PARTITION BY condition_id
                       ORDER BY hour_bucket
                       RANGE BETWEEN INTERVAL 24 HOUR PRECEDING AND CURRENT ROW
                   ) AS taker_vol_24h,
                   SUM(n_unique_takers) OVER (
                       PARTITION BY condition_id
                       ORDER BY hour_bucket
                       RANGE BETWEEN INTERVAL 24 HOUR PRECEDING AND CURRENT ROW
                   ) AS n_unique_takers_24h
              FROM read_parquet('{WALLET_FLOW_PARQUET}')
        )"""
        smart_money_join = """
        LEFT JOIN wallet_flow_24h wf
          ON wf.condition_id = s.condition_id
         AND wf.hour_bucket  = DATE_TRUNC('hour', TO_TIMESTAMP(s.trade_ts))"""

        if needs_smart_money:
            smart_money_select = """,
            CASE WHEN COALESCE(wf.taker_vol_24h, 0) > 0
                 THEN wf.whale_vol_24h / wf.taker_vol_24h
                 ELSE NULL
            END AS smart_money_taker_share"""
        if needs_n_takers_24h:
            smart_money_select += """,
            COALESCE(wf.n_unique_takers_24h, 0) AS n_takers_24h_before"""

    # Wallet quality at anchor (D14 — uses canonical wallet_score_at_anchor)
    wq_join = ""
    wq_select = ""
    if needs_wallet_quality:
        wq_join = f"""
        LEFT JOIN read_parquet('{WALLET_SCORE_PARQUET}') wq
          ON wq.condition_id = s.condition_id
         AND wq.anchor_hours = {anchor_h}"""
        # Also need buy_imbalance — compute from trade_buckets 24h pre-anchor
        wq_select = """,
            wq.taker_pnl_weighted_usd  AS taker_pnl_weighted_usd_24h,
            wq.taker_roi_weighted      AS taker_roi_weighted_24h,
            wq.n_takers_24h_before     AS wq_n_takers_24h_before,
            CASE WHEN COALESCE(v.volume_24h_running, 0) > 0
                 THEN v.buy_vol_24h_running / v.volume_24h_running
                 ELSE NULL
            END                        AS buy_imbalance_asof"""

    extra_cols_sql = (", " + ", ".join(extra_cols)) if extra_cols else ""
    extra_joins_sql = "\n        ".join(extra_joins)

    limit_sql = f"\n        LIMIT {limit}" if limit else ""

    return f"""
        WITH vol_24h AS (
            SELECT condition_id,
                   hour_bucket,
                   SUM(total_volume_usd) OVER (
                       PARTITION BY condition_id
                       ORDER BY hour_bucket
                       RANGE BETWEEN INTERVAL 24 HOUR PRECEDING AND CURRENT ROW
                   ) AS volume_24h_running,
                   SUM(buy_volume) OVER (
                       PARTITION BY condition_id
                       ORDER BY hour_bucket
                       RANGE BETWEEN INTERVAL 24 HOUR PRECEDING AND CURRENT ROW
                   ) AS buy_vol_24h_running
              FROM read_parquet('{TRADE_BUCKETS_PARQUET}')
        ){smart_money_cte}
        SELECT
            m.condition_id          AS market_id,
            m.event_id,
            m.category,
            m.event_title,
            CAST(m.neg_risk AS BOOLEAN)  AS neg_risk,
            CAST(m.horizon_days AS DOUBLE) AS horizon_days,
            CAST(m.lifetime_volume_usd AS DOUBLE) AS lifetime_volume_usd,
            m.yes_won,
            m.end_date,
            CAST(s.yes_price AS DOUBLE) AS yes_price,
            CAST(s.hours_to_resolve AS DOUBLE) AS hours_to_resolve,
            TO_TIMESTAMP(s.trade_ts) AS trade_ts,
            COALESCE(v.volume_24h_running, 0.0) AS volume_24h
            {extra_cols_sql}
            {smart_money_select}
            {wq_select}
        FROM read_parquet('{SNAPSHOTS_PARQUET}') s
        JOIN read_parquet('{MARKETS_PARQUET}') m
          ON m.condition_id = s.condition_id
        LEFT JOIN vol_24h v
          ON v.condition_id = s.condition_id
         AND v.hour_bucket  = DATE_TRUNC('hour', TO_TIMESTAMP(s.trade_ts))
        {smart_money_join}
        {wq_join}
        {extra_joins_sql}
        WHERE s.hours_to_resolve = {anchor_h}
          AND m.yes_won IS NOT NULL
          AND s.yes_price IS NOT NULL
        {limit_sql}
    """


# ── Adapters: parquet row → market_event dict ────────────────────────────
def _row_to_market_event(row: dict[str, Any], spec: SingleMarketSpec) -> dict[str, Any]:
    """Convert DuckDB row → market_event dict для detector.detect()."""
    me = {
        "market_id": row["market_id"],
        "event_id": row.get("event_id"),
        "category": row.get("category"),
        "event_title": row.get("event_title"),
        "current_price": float(row["yes_price"]),  # YES side
        "entry_price": float(row["yes_price"]),
        "volume_24h": float(row.get("volume_24h") or 0.0),
        "hours_to_resolve": float(row.get("hours_to_resolve") or 0.0),
        "horizon_days": float(row.get("horizon_days") or 0.0),
        "lifetime_volume_usd": float(row.get("lifetime_volume_usd") or 0.0),
        "clob_token_ids": None,  # not in HF dataset
    }
    if spec.needs_question:
        me["question"] = row.get("question")
    if spec.needs_smart_money:
        sm = row.get("smart_money_taker_share")
        me["smart_money_taker_share"] = float(sm) if sm is not None else None
    if spec.needs_n_takers_24h_before:
        me["n_takers_24h_before"] = int(row.get("n_takers_24h_before") or 0)
    if spec.needs_wallet_quality:
        pnl = row.get("taker_pnl_weighted_usd_24h")
        roi = row.get("taker_roi_weighted_24h")
        buy_imb = row.get("buy_imbalance_asof")
        n_takers = row.get("wq_n_takers_24h_before")
        me["taker_pnl_weighted_usd_24h"] = float(pnl) if pnl is not None else None
        me["taker_roi_weighted_24h"] = float(roi) if roi is not None else None
        me["buy_imbalance_asof"] = float(buy_imb) if buy_imb is not None else None
        me["n_takers_24h_before"] = int(n_takers) if n_takers is not None else None
    return me


# ── Outcome resolution ───────────────────────────────────────────────────
def _resolve_outcome(yes_won: int | None, side: str) -> str | None:
    """yes_won (0|1|None) + side (YES|NO) → outcome (WIN|LOSS|EXPIRED|None)."""
    if yes_won is None:
        return "EXPIRED"
    if side == "YES":
        return "WIN" if yes_won == 1 else "LOSS"
    # side == NO
    return "WIN" if yes_won == 0 else "LOSS"


def _resolved_at(end_date: Any) -> datetime:
    """Возвращает resolved_at = market end_date (approximation of resolution)."""
    if isinstance(end_date, datetime):
        return end_date if end_date.tzinfo else end_date.replace(tzinfo=UTC)
    # Fallback
    return datetime.now(UTC)


# ── Single-market backfill ───────────────────────────────────────────────
async def backfill_single_market(
    spec: SingleMarketSpec,
    *,
    dry_run: bool,
    limit: int | None,
) -> dict[str, int]:
    """Backfill один single-market детектор по всем anchors."""
    stats = {"snapshots_scanned": 0, "signals_emitted": 0, "inserts_done": 0, "errors": 0}

    import duckdb  # lazy: модуль есть только в Docker на проде

    con = duckdb.connect(":memory:")
    try:
        for anchor_h in spec.anchors_h:
            t0 = time.time()
            query = _build_snapshot_query(
                anchor_h=anchor_h,
                needs_question=spec.needs_question,
                needs_smart_money=spec.needs_smart_money,
                needs_n_takers_24h=spec.needs_n_takers_24h_before,
                needs_wallet_quality=spec.needs_wallet_quality,
                limit=limit,
            )
            try:
                rows = con.execute(query).fetchall()
            except Exception as exc:
                log.error("query_failed detector=%s anchor=%dh: %s", spec.name, anchor_h, exc)
                stats["errors"] += 1
                continue

            cols = [d[0] for d in con.description]
            log.info(
                "scanning detector=%s anchor=%dh rows=%d elapsed=%.1fs",
                spec.name, anchor_h, len(rows), time.time() - t0,
            )

            for raw in rows:
                stats["snapshots_scanned"] += 1
                row = dict(zip(cols, raw))
                try:
                    me = _row_to_market_event(row, spec)
                    sig = spec.detect_fn(me)
                except Exception as exc:
                    log.warning("detect_failed market=%s: %s", row.get("market_id"), exc)
                    stats["errors"] += 1
                    continue

                if sig is None:
                    continue

                stats["signals_emitted"] += 1

                # Generated_at = trade_ts из snapshot (исторический момент)
                gen_at = row["trade_ts"]
                if isinstance(gen_at, datetime) and gen_at.tzinfo is None:
                    gen_at = gen_at.replace(tzinfo=UTC)

                outcome = _resolve_outcome(row.get("yes_won"), sig.side)
                resolved_at = _resolved_at(row.get("end_date"))
                roi_gross = _realized_roi_pct(
                    outcome or "EXPIRED",
                    sig.current_price,
                    sig.side,
                )
                roi_net = _realized_roi_net_pct(
                    outcome or "EXPIRED",
                    sig.current_price,
                    sig.side,
                    row.get("category"),
                )

                evidence = dict(sig.evidence)
                evidence["backfill"] = True
                evidence["backfill_anchor_h"] = anchor_h
                evidence["tx_hash"] = f"backfill_{spec.name}_{sig.market_id}_{anchor_h}h"

                if dry_run:
                    if stats["signals_emitted"] <= 5:
                        log.info(
                            "[DRY] %s market=%s side=%s entry=%.3f outcome=%s roi_gross=%+.1f roi_net=%+.1f",
                            spec.name, sig.market_id, sig.side, sig.current_price,
                            outcome, roi_gross, roi_net,
                        )
                else:
                    try:
                        await _db_execute(
                            _INSERT_SQL,
                            sig.detector,
                            sig.market_id,
                            sig.event_id,
                            sig.side,
                            sig.confidence,
                            sig.expected_edge_pct,
                            sig.fair_price,
                            sig.current_price,
                            sig.horizon_sec,
                            sig.token_id,
                            __import__("json").dumps(evidence),
                            gen_at,
                            outcome,
                            roi_gross,
                            roi_net,
                            resolved_at,
                        )
                        stats["inserts_done"] += 1
                    except Exception as exc:
                        log.warning("insert_failed market=%s: %s", sig.market_id, exc)
                        stats["errors"] += 1
    finally:
        con.close()
    return stats


# ── Event-level backfill ─────────────────────────────────────────────────
async def backfill_d3_or_d29(*, detector_name: str, dry_run: bool, limit: int | None) -> dict[str, int]:
    """D3 (cross_mkt_arb, neg_risk basket) + D29 (negrisk basket arb).

    Оба требуют neg_risk events с n_markets>=2 (D3) / >=3 (D29).
    """
    stats = {"events_scanned": 0, "signals_emitted": 0, "inserts_done": 0, "errors": 0}

    is_d3 = detector_name == "D3"
    min_markets = 2 if is_d3 else 3
    short = "D3" if is_d3 else "D29"
    detect_fn = d3_cross_mkt_arb.detect if is_d3 else d29_negrisk_basket_arb.detect

    import duckdb  # lazy: модуль есть только в Docker на проде

    con = duckdb.connect(":memory:")
    try:
        limit_sql = f"LIMIT {limit}" if limit else ""
        query = f"""
            SELECT
                m.event_id,
                s.hours_to_resolve,
                TO_TIMESTAMP(s.trade_ts) AS trade_ts,
                LIST(m.condition_id)                                         AS market_ids,
                LIST(m.event_title)                                          AS event_titles,
                LIST(m.category)                                             AS categories,
                LIST(CAST(s.yes_price AS DOUBLE))                            AS yes_prices,
                LIST(m.yes_won)                                              AS yes_wons,
                LIST(m.end_date)                                             AS end_dates,
                LIST(m.lifetime_volume_usd)                                  AS volumes,
                COUNT(*)                                                     AS n_markets,
                SUM(s.yes_price)                                             AS sum_yes,
                BOOL_AND(CAST(m.neg_risk AS BOOLEAN))                        AS all_neg_risk
              FROM read_parquet('{SNAPSHOTS_PARQUET}') s
              JOIN read_parquet('{MARKETS_PARQUET}') m
                ON m.condition_id = s.condition_id
             WHERE s.hours_to_resolve IN (4, 12, 24, 72, 168)
               AND s.yes_price IS NOT NULL
             GROUP BY m.event_id, s.hours_to_resolve, s.trade_ts
            HAVING COUNT(*) >= {min_markets}
               AND BOOL_AND(CAST(m.neg_risk AS BOOLEAN)) = true
            {limit_sql}
        """
        try:
            rows = con.execute(query).fetchall()
        except Exception as exc:
            log.error("%s_query_failed: %s", short, exc)
            return stats
        cols = [d[0] for d in con.description]
        log.info("%s candidate neg_risk events: %d", short, len(rows))

        for raw in rows:
            stats["events_scanned"] += 1
            row = dict(zip(cols, raw))
            market_ids = row["market_ids"]
            yes_prices = [float(p) for p in row["yes_prices"]]
            yes_wons = row["yes_wons"]
            end_dates = row["end_dates"]
            categories = row["categories"]
            event_titles = row["event_titles"]
            volumes = row["volumes"]
            anchor_h = int(row["hours_to_resolve"])
            gen_at = row["trade_ts"]
            if isinstance(gen_at, datetime) and gen_at.tzinfo is None:
                gen_at = gen_at.replace(tzinfo=UTC)

            try:
                if is_d3:
                    # D3 takes event_id + Sequence[EventMarket]
                    em_list = [
                        d3_cross_mkt_arb.EventMarket(market_id=str(mid), yes_price=yp, token_id=None)
                        for mid, yp in zip(market_ids, yes_prices)
                    ]
                    signals = detect_fn(
                        event_id=row.get("event_id") or "",
                        markets=em_list,
                        neg_risk=True,
                        neg_risk_other=False,  # canonical HF не несёт это поле
                        n_yes_winners=1,
                    )
                    if not signals:
                        continue
                    sig = signals[0]
                    # target_idx = market с max yes_price
                    target_idx = max(range(len(yes_prices)), key=lambda i: yes_prices[i])
                else:
                    # D29 basket_event
                    basket_markets = [
                        {
                            "market_id": str(mid),
                            "yes_price": yp,
                            "clob_token_ids": None,
                            "volume_24h": float(vol or 0.0),
                            "end_date": ed,
                            "category": cat,
                        }
                        for mid, yp, vol, ed, cat in zip(
                            market_ids, yes_prices, volumes, end_dates, categories,
                        )
                    ]
                    basket_event = {
                        "event_id": row.get("event_id"),
                        "event_title": event_titles[0] if event_titles else None,
                        "markets": basket_markets,
                    }
                    sig = detect_fn(basket_event)
                    if sig is None:
                        continue
                    # D29 picks min yes_price as primary
                    target_idx = min(range(len(yes_prices)), key=lambda i: yes_prices[i])
            except Exception as exc:
                log.warning("%s_detect_failed event=%s: %s", short, row.get("event_id"), exc)
                stats["errors"] += 1
                continue

            stats["signals_emitted"] += 1
            outcome = _resolve_outcome(yes_wons[target_idx], sig.side)
            resolved_at = _resolved_at(end_dates[target_idx])
            roi_gross = _realized_roi_pct(outcome or "EXPIRED", sig.current_price, sig.side)
            roi_net = _realized_roi_net_pct(
                outcome or "EXPIRED", sig.current_price, sig.side, categories[target_idx],
            )
            evidence = dict(sig.evidence)
            evidence["backfill"] = True
            evidence["backfill_anchor_h"] = anchor_h
            evidence["tx_hash"] = f"backfill_{short}_{sig.market_id}_{anchor_h}h"

            if dry_run:
                if stats["signals_emitted"] <= 5:
                    log.info("[DRY] %s market=%s side=%s outcome=%s roi_net=%+.1f",
                             short, sig.market_id, sig.side, outcome, roi_net)
            else:
                try:
                    await _db_execute(
                        _INSERT_SQL,
                        sig.detector, sig.market_id, sig.event_id, sig.side,
                        sig.confidence, sig.expected_edge_pct, sig.fair_price, sig.current_price,
                        sig.horizon_sec, sig.token_id,
                        __import__("json").dumps(evidence), gen_at,
                        outcome, roi_gross, roi_net, resolved_at,
                    )
                    stats["inserts_done"] += 1
                except Exception as exc:
                    log.warning("%s_insert_failed: %s", short, exc)
                    stats["errors"] += 1
    finally:
        con.close()
    return stats


# ── Main ─────────────────────────────────────────────────────────────────
async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--detector", default="all",
        help=f"One of {ALL_DETECTORS} or 'all'",
    )
    parser.add_argument("--dry-run", action="store_true", help="Не писать в БД, только preview")
    parser.add_argument("--limit", type=int, default=None, help="LIMIT на DuckDB queries (для smoke-test)")
    args = parser.parse_args()

    # Sanity: canonical layer должен существовать
    for path in (MARKETS_PARQUET, SNAPSHOTS_PARQUET, TRADE_BUCKETS_PARQUET):
        if not path.exists():
            log.error("Canonical file missing: %s — backfill cannot run", path)
            sys.exit(1)

    targets: list[str]
    if args.detector == "all":
        targets = list(ALL_DETECTORS)
    elif args.detector in ALL_DETECTORS:
        targets = [args.detector]
    else:
        log.error("Unknown detector: %s (allowed: %s)", args.detector, ALL_DETECTORS)
        sys.exit(2)

    if not args.dry_run:
        await _db_init()

    try:
        grand_total = {"snapshots_scanned": 0, "signals_emitted": 0, "inserts_done": 0, "errors": 0,
                       "events_scanned": 0}
        for tgt in targets:
            log.info("══════════ %s ══════════", tgt)
            t0 = time.time()
            if tgt in SINGLE_MARKET_SPECS:
                stats = await backfill_single_market(
                    SINGLE_MARKET_SPECS[tgt],
                    dry_run=args.dry_run,
                    limit=args.limit,
                )
            elif tgt in ("D3", "D29"):
                stats = await backfill_d3_or_d29(
                    detector_name=tgt, dry_run=args.dry_run, limit=args.limit,
                )
            else:
                log.warning("Skipping unsupported detector: %s", tgt)
                continue

            elapsed = time.time() - t0
            log.info("%s done in %.1fs: %s", tgt, elapsed, stats)
            for k, v in stats.items():
                grand_total[k] = grand_total.get(k, 0) + v

        log.info("═════════ TOTAL ═════════ %s", grand_total)
    finally:
        if not args.dry_run:
            await _db_close()


if __name__ == "__main__":
    asyncio.run(main())
