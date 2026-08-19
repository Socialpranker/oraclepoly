"""Feature kernel — single source of truth для 55-col ml_features SQL.

Используется обоими builders:
  • Canonical batch (scripts/research/build_ml_features.py) — DuckDB поверх parquet,
    market_filter='WHERE m.yes_won IS NOT NULL' (только resolved markets для training).
  • Live incremental (app/jobs/live_ml_features_builder.py) — Postgres поверх live tables,
    market_filter='WHERE m.is_active AND m.resolved=false' (только active markets для inference).

КАЖДЫЙ ROW = (condition_id, anchor_hours) — точка решения "за N часов до резолва".
Все features as_of anchor_ts = end_date - anchor_hours. Никакого post-anchor leakage.

Source-agnostic: SQL принимает имена tables (Postgres) или read_parquet('path') strings (DuckDB).
"""

from __future__ import annotations

CANONICAL_ANCHORS: list[int] = [4, 24, 72, 168, 720]


def _resolve_source(src: str, *, is_duckdb: bool) -> str:
    """For DuckDB use read_parquet('<path>'), for Postgres just <table_name>.

    Если caller уже передал read_parquet(...) или query — используем как есть.
    Если src выглядит как path (содержит '/' или '.parquet') и is_duckdb=True →
    оборачиваем в read_parquet. Если просто table name (как в тестах) — leave as-is.
    """
    s = src.strip()
    if s.startswith("read_parquet(") or s.startswith("("):
        return s
    if is_duckdb and ("/" in s or s.endswith(".parquet")):
        return f"read_parquet('{s}')"
    return s


def build_features_sql(
    *,
    markets_src: str,
    snaps_src: str,
    buckets_src: str,
    wflow_src: str,
    anchor_h: int,
    market_filter: str = "WHERE m.yes_won IS NOT NULL",
    is_duckdb: bool = True,
) -> str:
    """Return SQL string producing 55-col feature matrix for one anchor.

    Parameters
    ----------
    markets_src, snaps_src, buckets_src, wflow_src
        Имена Postgres tables ИЛИ paths к parquet. Если path — обернётся в read_parquet().
    anchor_h
        Один из CANONICAL_ANCHORS. Builder вызывает функцию по разу на каждый.
    market_filter
        Полная WHERE clause (с keyword 'WHERE'). По умолчанию canonical resolved-only.
        Live: "WHERE m.is_active AND m.resolved=false AND m.end_date - INTERVAL '{h} hours' <= now()"
    is_duckdb
        True для DuckDB (parquet), False для Postgres. Влияет на:
          - оборачивание sources в read_parquet()
          - INTERVAL syntax (DuckDB: INTERVAL ('24 hours')::INTERVAL, PG: INTERVAL '24 hours')
          - cast syntax (DuckDB: ::INT, PG: ::int одинаково ок)

    Returns
    -------
    SQL string. Caller сам делает COPY ... TO parquet (DuckDB) или INSERT ... ON CONFLICT (PG).
    """
    S = _resolve_source(snaps_src, is_duckdb=is_duckdb)
    B = _resolve_source(buckets_src, is_duckdb=is_duckdb)
    W = _resolve_source(wflow_src, is_duckdb=is_duckdb)

    # Markets source: для Postgres проектируем canonical-style columns над production schema.
    # Postgres markets table не имеет yes_won (target недоступен для active markets),
    # horizon_days (есть horizon column другого типа), neg_risk (вообще нет).
    # Эти колонки вычисляются:
    #   yes_won       — NULL для active (target доступен только после resolve)
    #   horizon_days  — EXTRACT(EPOCH FROM end_date - created_at) / 86400
    #   neg_risk      — FALSE (proxy; production пока не tracks negrisk на market level)
    # См. plans/bright-hopping-meteor.md (Day 5 risks).
    if is_duckdb:
        M = _resolve_source(markets_src, is_duckdb=True)
    else:
        # Postgres: оборачиваем в подзапрос с derived columns
        raw = _resolve_source(markets_src, is_duckdb=False)
        M = f"""(
            SELECT
                {raw}.condition_id,
                {raw}.event_id,
                {raw}.end_date,
                {raw}.category,
                {raw}.is_active,
                {raw}.resolved,
                {raw}.volume_24h,
                {raw}.clob_token_ids,
                NULL::boolean AS yes_won,
                FALSE AS neg_risk,
                EXTRACT(EPOCH FROM ({raw}.end_date - {raw}.created_at)) / 86400.0 AS horizon_days
            FROM {raw}
        )"""

    # INTERVAL syntax: DuckDB требует строкового интервала с явным cast,
    # Postgres принимает literal INTERVAL '24 hours'.
    # Используем форму совместимую с обоими: INTERVAL '<n> hours' (PG-style) работает в DuckDB
    # начиная с v0.7+. Builder canonical уже использует "INTERVAL ({anchor_h} || ' hours')::INTERVAL"
    # — это DuckDB-специфично из-за параметризации через ||. Для kernel унифицируем:
    # сразу подставляем числовое значение в литерал.
    INT_ANCHOR = f"INTERVAL '{anchor_h} hours'"
    INT_24H = "INTERVAL '24 hours'"

    # Lifetime volume column: только в canonical parquet (markets_clean.parquet).
    # В Postgres markets table нет — используем volume_24h как proxy.
    # Это semantic divergence (24h vs lifetime) которую feature_parity_check.py
    # детектирует для event_* колонок. Принятый tradeoff — иначе нужна миграция
    # Postgres с добавлением lifetime_volume_usd + populate из aggregations.
    # ВАЖНО: используем простое имя колонки (не выражение) чтобы можно было
    # писать `m.{LIFETIME_VOL_COL}` без скобок.
    LIFETIME_VOL_COL = "lifetime_volume_usd" if is_duckdb else "volume_24h"

    # EXTRACT(DOW FROM ...) — DuckDB и Postgres совпадают
    # EXTRACT(EPOCH FROM ...) — совпадают
    # bool::INT vs bool::int — совпадают
    return f"""
WITH
-- Snapshot price for THIS anchor
anchor_price AS (
    SELECT condition_id, yes_price
    FROM {S}
    WHERE hours_to_resolve = {anchor_h}
),
-- Earlier snapshots (still in past relative to anchor)
earlier_snaps AS (
    SELECT
        condition_id,
        MAX(CASE WHEN hours_to_resolve = 24  THEN yes_price END) AS yes_price_24h,
        MAX(CASE WHEN hours_to_resolve = 72  THEN yes_price END) AS yes_price_72h,
        MAX(CASE WHEN hours_to_resolve = 168 THEN yes_price END) AS yes_price_168h,
        MAX(CASE WHEN hours_to_resolve = 720 THEN yes_price END) AS yes_price_720h
    FROM {S}
    WHERE hours_to_resolve >= {anchor_h}
    GROUP BY condition_id
),
-- Trade buckets aggregated AS_OF anchor_ts
buckets_asof AS (
    SELECT
        b.condition_id,
        COUNT(*)                                  AS n_buckets_so_far,
        SUM(b.n_trades)                           AS n_trades_asof,
        SUM(b.total_volume_usd)                   AS volume_asof,
        SUM(b.vwap_yes * b.total_volume_usd) /
            NULLIF(SUM(b.total_volume_usd), 0)    AS vwap_asof,
        MIN(b.min_yes_price)                      AS price_min_asof,
        MAX(b.max_yes_price)                      AS price_max_asof,
        SUM(b.buy_volume) /
            NULLIF(SUM(b.buy_volume + b.sell_volume), 0) AS buy_ratio_asof,
        SUM(CASE WHEN EXTRACT(EPOCH FROM (m.end_date - {INT_ANCHOR} - b.hour_bucket))/3600.0
                      BETWEEN 0 AND 24 THEN b.total_volume_usd ELSE 0 END) AS vol_24h_before_anchor,
        SUM(CASE WHEN EXTRACT(EPOCH FROM (m.end_date - {INT_ANCHOR} - b.hour_bucket))/3600.0
                      BETWEEN 0 AND 24 THEN b.n_trades ELSE 0 END) AS trades_24h_before_anchor,
        SUM(CASE WHEN EXTRACT(EPOCH FROM (m.end_date - {INT_ANCHOR} - b.hour_bucket))/3600.0
                      BETWEEN 0 AND 24 THEN b.buy_volume ELSE 0 END) /
            NULLIF(SUM(CASE WHEN EXTRACT(EPOCH FROM (m.end_date - {INT_ANCHOR} - b.hour_bucket))/3600.0
                                  BETWEEN 0 AND 24 THEN b.buy_volume + b.sell_volume ELSE 0 END), 0) AS buy_ratio_24h_before_anchor,
        SUM(CASE WHEN EXTRACT(EPOCH FROM (m.end_date - {INT_ANCHOR} - b.hour_bucket))/3600.0
                      BETWEEN 0 AND 72 THEN b.total_volume_usd ELSE 0 END) AS vol_72h_before_anchor,
        SUM(CASE WHEN EXTRACT(EPOCH FROM (m.end_date - {INT_ANCHOR} - b.hour_bucket))/3600.0
                      BETWEEN 0 AND 72 THEN b.buy_volume ELSE 0 END) /
            NULLIF(SUM(CASE WHEN EXTRACT(EPOCH FROM (m.end_date - {INT_ANCHOR} - b.hour_bucket))/3600.0
                                  BETWEEN 0 AND 72 THEN b.buy_volume + b.sell_volume ELSE 0 END), 0) AS buy_ratio_72h_before_anchor
    FROM {B} b
    JOIN {M} m ON m.condition_id = b.condition_id
    WHERE b.hour_bucket <= m.end_date - {INT_ANCHOR}
    GROUP BY b.condition_id
),
-- ─── PHASE B: Microstructure (vol, drawdown, gaps) ───
microstructure AS (
    SELECT
        b.condition_id,
        STDDEV_POP(CASE WHEN EXTRACT(EPOCH FROM (m.end_date - {INT_ANCHOR} - b.hour_bucket))/3600.0
                             BETWEEN 0 AND 24 THEN b.vwap_yes END) AS realized_vol_24h,
        STDDEV_POP(CASE WHEN EXTRACT(EPOCH FROM (m.end_date - {INT_ANCHOR} - b.hour_bucket))/3600.0
                             BETWEEN 0 AND 72 THEN b.vwap_yes END) AS realized_vol_72h,
        MAX(CASE WHEN EXTRACT(EPOCH FROM (m.end_date - {INT_ANCHOR} - b.hour_bucket))/3600.0
                      BETWEEN 0 AND 24 THEN b.max_yes_price END) AS pmax_24h,
        MIN(CASE WHEN EXTRACT(EPOCH FROM (m.end_date - {INT_ANCHOR} - b.hour_bucket))/3600.0
                      BETWEEN 0 AND 24 THEN b.min_yes_price END) AS pmin_24h,
        SUM(ABS(b.buy_volume - b.sell_volume)) /
            NULLIF(SUM(b.buy_volume + b.sell_volume), 0) AS vpin_proxy_lifetime,
        SUM(b.max_yes_price - b.min_yes_price) /
            NULLIF(SQRT(SUM(b.total_volume_usd)), 0) AS kyle_lambda_proxy
    FROM {B} b
    JOIN {M} m ON m.condition_id = b.condition_id
    WHERE b.hour_bucket <= m.end_date - {INT_ANCHOR}
    GROUP BY b.condition_id
),
-- ─── PHASE A: Wallet-level signal ───
wallet_flow AS (
    SELECT
        wf.condition_id,
        SUM(CASE WHEN wf.hour_bucket >= m.end_date - {INT_ANCHOR} - {INT_24H}
                  AND wf.hour_bucket <  m.end_date - {INT_ANCHOR}
                 THEN wf.n_unique_takers ELSE 0 END) AS n_unique_takers_24h,
        SUM(CASE WHEN wf.hour_bucket >= m.end_date - {INT_ANCHOR} - {INT_24H}
                  AND wf.hour_bucket <  m.end_date - {INT_ANCHOR}
                 THEN wf.n_unique_makers ELSE 0 END) AS n_unique_makers_24h,
        SUM(CASE WHEN wf.hour_bucket >= m.end_date - {INT_ANCHOR} - {INT_24H}
                  AND wf.hour_bucket <  m.end_date - {INT_ANCHOR}
                 THEN wf.whale_taker_volume ELSE 0 END) AS whale_taker_volume_24h,
        SUM(CASE WHEN wf.hour_bucket >= m.end_date - {INT_ANCHOR} - {INT_24H}
                  AND wf.hour_bucket <  m.end_date - {INT_ANCHOR}
                 THEN wf.whale_taker_count ELSE 0 END) AS whale_taker_count_24h,
        AVG(CASE WHEN wf.hour_bucket >= m.end_date - {INT_ANCHOR} - {INT_24H}
                  AND wf.hour_bucket <  m.end_date - {INT_ANCHOR}
                 THEN wf.retail_taker_avg_size END) AS retail_taker_avg_size_24h,
        SUM(CASE WHEN wf.hour_bucket >= m.end_date - {INT_ANCHOR} - {INT_24H}
                  AND wf.hour_bucket <  m.end_date - {INT_ANCHOR}
                 THEN wf.whale_buy_pressure ELSE 0 END) AS whale_buy_pressure_24h,
        1.0 / NULLIF(SUM(CASE WHEN wf.hour_bucket >= m.end_date - {INT_ANCHOR} - {INT_24H}
                                AND wf.hour_bucket <  m.end_date - {INT_ANCHOR}
                               THEN wf.n_unique_takers ELSE 0 END), 0) AS taker_hhi_proxy_24h,
        SUM(CASE WHEN wf.hour_bucket >= m.end_date - {INT_ANCHOR} - {INT_24H}
                  AND wf.hour_bucket <  m.end_date - {INT_ANCHOR}
                 THEN wf.taker_count_total ELSE 0 END)::DOUBLE PRECISION /
            NULLIF(SUM(CASE WHEN wf.hour_bucket >= m.end_date - {INT_ANCHOR} - {INT_24H}
                              AND wf.hour_bucket <  m.end_date - {INT_ANCHOR}
                             THEN wf.n_unique_takers ELSE 0 END), 0) AS repeat_taker_ratio_24h
    FROM {W} wf
    JOIN {M} m ON m.condition_id = wf.condition_id
    WHERE wf.hour_bucket <= m.end_date - {INT_ANCHOR}
    GROUP BY wf.condition_id
),
-- ─── PHASE C: Event-level features ───
event_stats AS (
    SELECT
        event_id,
        COUNT(*) AS n_markets_in_event,
        SUM({LIFETIME_VOL_COL}) AS event_total_lifetime_volume,
        MIN({LIFETIME_VOL_COL}) AS event_thinnest_market_vol,
        MAX({LIFETIME_VOL_COL}) AS event_largest_market_vol,
        MAX({LIFETIME_VOL_COL}) / NULLIF(SUM({LIFETIME_VOL_COL}), 0) AS event_largest_market_share
    FROM {M}
    WHERE event_id IS NOT NULL
    GROUP BY event_id
),
event_arb AS (
    SELECT
        m.event_id,
        SUM(ap.yes_price) AS event_yes_sum,
        SUM(1.0 - ap.yes_price) AS event_no_sum,
        ABS(SUM(ap.yes_price) - 1.0) AS event_arb_distance_yes
    FROM {M} m
    JOIN anchor_price ap ON ap.condition_id = m.condition_id
    WHERE m.event_id IS NOT NULL
    GROUP BY m.event_id
)
SELECT
    m.condition_id,
    m.event_id,
    {anchor_h}::INT                                AS anchor_hours,
    m.yes_won,                                     -- TARGET (NULL для active markets)

    -- Static metadata
    m.category,
    m.neg_risk::INT                                AS neg_risk,
    m.horizon_days,
    EXTRACT(HOUR FROM m.end_date)::INT             AS resolve_hour_utc,
    EXTRACT(DOW  FROM m.end_date)::INT             AS resolve_dow,
    EXTRACT(MONTH FROM m.end_date)::INT            AS resolve_month,
    EXTRACT(YEAR FROM m.end_date)::INT             AS resolve_year,
    COALESCE(es.n_markets_in_event, 1)             AS n_markets_in_event,

    -- Anchor price
    ap.yes_price                                   AS yes_price_at_anchor,

    -- Earlier snapshots
    es_p.yes_price_24h,
    es_p.yes_price_72h,
    es_p.yes_price_168h,
    es_p.yes_price_720h,

    -- Drift
    ap.yes_price - es_p.yes_price_72h              AS drift_72h_to_anchor,
    ap.yes_price - es_p.yes_price_168h             AS drift_7d_to_anchor,

    -- As-of cumulative aggregates
    ba.n_buckets_so_far                            AS hours_with_trades_asof,
    ba.n_trades_asof,
    ba.volume_asof,
    ba.vwap_asof,
    ba.price_min_asof,
    ba.price_max_asof,
    ba.price_max_asof - ba.price_min_asof          AS price_range_asof,
    ba.buy_ratio_asof,
    ba.buy_ratio_asof - 0.5                        AS buy_imbalance_asof,

    -- Recent flow
    ba.vol_24h_before_anchor,
    ba.trades_24h_before_anchor,
    ba.vol_72h_before_anchor,
    ba.buy_ratio_24h_before_anchor,
    ba.buy_ratio_72h_before_anchor,

    -- Velocity ratios
    ba.vol_24h_before_anchor / NULLIF(ba.volume_asof, 0)  AS vol_24h_pct_asof,
    ba.vol_72h_before_anchor / NULLIF(ba.volume_asof, 0)  AS vol_72h_pct_asof,

    -- ─── PHASE B: Microstructure (7) ───
    ms.realized_vol_24h,
    ms.realized_vol_72h,
    CASE WHEN ms.pmax_24h > 0 THEN (ms.pmax_24h - ms.pmin_24h) / ms.pmax_24h ELSE 0 END AS max_drawdown_24h_pct,
    ms.vpin_proxy_lifetime,
    ms.kyle_lambda_proxy,
    ba.n_buckets_so_far::DOUBLE PRECISION / NULLIF(ms.realized_vol_24h, 0)  AS liquidity_quality_proxy,
    CASE WHEN ba.volume_asof > 0
         THEN ba.vol_24h_before_anchor / ba.volume_asof
         ELSE 0 END AS vol_24h_concentration,

    -- ─── PHASE A: Wallet-level signal (8) ───
    COALESCE(wf.n_unique_takers_24h, 0)            AS n_unique_takers_24h,
    COALESCE(wf.n_unique_makers_24h, 0)            AS n_unique_makers_24h,
    COALESCE(wf.whale_taker_volume_24h, 0.0)       AS whale_taker_volume_24h,
    COALESCE(wf.whale_taker_count_24h, 0)          AS whale_taker_count_24h,
    COALESCE(wf.retail_taker_avg_size_24h, 0.0)    AS retail_taker_avg_size_24h,
    COALESCE(wf.whale_buy_pressure_24h, 0.0)       AS whale_buy_pressure_24h,
    COALESCE(wf.taker_hhi_proxy_24h, 0.0)          AS taker_hhi_proxy_24h,
    COALESCE(wf.repeat_taker_ratio_24h, 0.0)       AS repeat_taker_ratio_24h,

    -- ─── PHASE C: Event-level (5) ───
    COALESCE(es.event_total_lifetime_volume, m.{LIFETIME_VOL_COL}) AS event_total_lifetime_volume,
    COALESCE(es.event_thinnest_market_vol, m.{LIFETIME_VOL_COL}) AS event_thinnest_market_vol,
    COALESCE(es.event_largest_market_share, 1.0)   AS event_largest_market_share,
    COALESCE(ea.event_yes_sum, ap.yes_price)       AS event_yes_sum,
    COALESCE(ea.event_arb_distance_yes, 0.0)       AS event_arb_distance_yes
FROM {M} m
JOIN anchor_price       ap   ON ap.condition_id   = m.condition_id
LEFT JOIN earlier_snaps es_p ON es_p.condition_id = m.condition_id
LEFT JOIN buckets_asof  ba   ON ba.condition_id   = m.condition_id
LEFT JOIN microstructure ms  ON ms.condition_id   = m.condition_id
LEFT JOIN wallet_flow   wf   ON wf.condition_id   = m.condition_id
LEFT JOIN event_stats   es   ON es.event_id       = m.event_id
LEFT JOIN event_arb     ea   ON ea.event_id       = m.event_id
{market_filter}
"""


# Public API: список колонок которые produce SQL (в порядке SELECT).
# Используется для assert'ов в predictor что schema совпадает с model.feat_cols.
FEATURE_COLUMNS: list[str] = [
    "condition_id",
    "event_id",
    "anchor_hours",
    "yes_won",
    "category",
    "neg_risk",
    "horizon_days",
    "resolve_hour_utc",
    "resolve_dow",
    "resolve_month",
    "resolve_year",
    "n_markets_in_event",
    "yes_price_at_anchor",
    "yes_price_24h",
    "yes_price_72h",
    "yes_price_168h",
    "yes_price_720h",
    "drift_72h_to_anchor",
    "drift_7d_to_anchor",
    "hours_with_trades_asof",
    "n_trades_asof",
    "volume_asof",
    "vwap_asof",
    "price_min_asof",
    "price_max_asof",
    "price_range_asof",
    "buy_ratio_asof",
    "buy_imbalance_asof",
    "vol_24h_before_anchor",
    "trades_24h_before_anchor",
    "vol_72h_before_anchor",
    "buy_ratio_24h_before_anchor",
    "buy_ratio_72h_before_anchor",
    "vol_24h_pct_asof",
    "vol_72h_pct_asof",
    "realized_vol_24h",
    "realized_vol_72h",
    "max_drawdown_24h_pct",
    "vpin_proxy_lifetime",
    "kyle_lambda_proxy",
    "liquidity_quality_proxy",
    "vol_24h_concentration",
    "n_unique_takers_24h",
    "n_unique_makers_24h",
    "whale_taker_volume_24h",
    "whale_taker_count_24h",
    "retail_taker_avg_size_24h",
    "whale_buy_pressure_24h",
    "taker_hhi_proxy_24h",
    "repeat_taker_ratio_24h",
    "event_total_lifetime_volume",
    "event_thinnest_market_vol",
    "event_largest_market_share",
    "event_yes_sum",
    "event_arb_distance_yes",
]
