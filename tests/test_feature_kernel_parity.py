"""Parity test для feature_kernel.

Подход: создаём synthetic mini-dataset (4 markets × несколько hourly buckets) →
прогоняем через build_features_sql() в DuckDB → проверяем:
  1. Все 55 колонок присутствуют + порядок совпадает с FEATURE_COLUMNS
  2. Один row per (condition_id, anchor_hours) для resolved markets
  3. yes_won правильно protruded из markets
  4. anchor_hours имеет правильное значение
  5. yes_price_at_anchor берётся из правильного snapshot
  6. Aggregates (volume_asof, n_trades_asof) считаются по pre-anchor buckets

Live mode test: market_filter='WHERE m.is_active=true' с NULL yes_won — должен работать
без ошибок (NULL в target колонке OK).
"""

from __future__ import annotations

import duckdb
import pytest

from app.ml.feature_kernel import FEATURE_COLUMNS, build_features_sql


@pytest.fixture
def synthetic_db():
    """4 markets, 2 events. Anchor=24h. Buckets за 30h до резолва."""
    con = duckdb.connect(":memory:")

    # Markets table — 2 resolved (WIN/LOSS), 2 active (yes_won NULL).
    # Включаем все колонки которые kernel может использовать в любом mode:
    #   canonical: yes_won, lifetime_volume_usd, neg_risk, horizon_days
    #   postgres:  volume_24h, is_active, resolved, clob_token_ids, created_at
    con.execute("""
        CREATE TABLE markets AS
        SELECT * FROM (VALUES
            ('cid_resolved_win',  'evt_a', TRUE,  TIMESTAMP '2026-05-01 00:00:00', TIMESTAMP '2026-04-26 00:00:00', 'Sports',   FALSE, 5.0,  100000.0, 100000.0, FALSE, TRUE,  '["t1","t2"]'),
            ('cid_resolved_loss', 'evt_a', FALSE, TIMESTAMP '2026-05-01 00:00:00', TIMESTAMP '2026-04-26 00:00:00', 'Sports',   FALSE, 5.0,  80000.0,  80000.0,  FALSE, TRUE,  '["t3","t4"]'),
            ('cid_active_1',      'evt_b', NULL,  TIMESTAMP '2026-05-10 00:00:00', TIMESTAMP '2026-04-30 00:00:00', 'Politics', TRUE,  10.0, 50000.0,  50000.0,  TRUE,  FALSE, '["t5","t6"]'),
            ('cid_active_2',      'evt_b', NULL,  TIMESTAMP '2026-05-10 00:00:00', TIMESTAMP '2026-04-30 00:00:00', 'Politics', TRUE,  10.0, 30000.0,  30000.0,  TRUE,  FALSE, '["t7","t8"]')
        ) AS t(condition_id, event_id, yes_won, end_date, created_at, category, neg_risk, horizon_days, lifetime_volume_usd, volume_24h, is_active, resolved, clob_token_ids)
    """)

    # Snapshots: для каждого market price на anchors 24h, 72h, 168h, 720h
    con.execute("""
        CREATE TABLE snaps AS
        SELECT * FROM (VALUES
            ('cid_resolved_win',  24,  0.85),
            ('cid_resolved_win',  72,  0.75),
            ('cid_resolved_win',  168, 0.60),
            ('cid_resolved_win',  720, 0.50),
            ('cid_resolved_loss', 24,  0.20),
            ('cid_resolved_loss', 72,  0.30),
            ('cid_resolved_loss', 168, 0.40),
            ('cid_resolved_loss', 720, 0.50),
            ('cid_active_1',      24,  0.90),
            ('cid_active_1',      72,  0.80),
            ('cid_active_1',      168, 0.70),
            ('cid_active_1',      720, 0.50),
            ('cid_active_2',      24,  0.10),
            ('cid_active_2',      72,  0.20),
            ('cid_active_2',      168, 0.30),
            ('cid_active_2',      720, 0.50)
        ) AS t(condition_id, hours_to_resolve, yes_price)
    """)

    # Buckets: для cid_resolved_win 3 bucket'а до anchor=24h (т.е. до 2026-04-30 00:00).
    # bucket times: 2026-04-29 23:00 (1h before anchor), 22:00 (2h), 21:00 (3h).
    # Используем явные DOUBLE/INT типы чтобы не получить inferred DECIMAL(4,1).
    con.execute("""
        CREATE TABLE buckets (
            condition_id TEXT,
            hour_bucket TIMESTAMP,
            n_trades INT,
            total_volume_usd DOUBLE,
            vwap_yes DOUBLE,
            min_yes_price DOUBLE,
            max_yes_price DOUBLE,
            buy_volume DOUBLE,
            sell_volume DOUBLE
        )
    """)
    con.execute("""
        INSERT INTO buckets VALUES
            ('cid_resolved_win',  TIMESTAMP '2026-04-29 23:00:00', 10, 1000.0, 0.84, 0.83, 0.85, 600.0, 400.0),
            ('cid_resolved_win',  TIMESTAMP '2026-04-29 22:00:00', 8,  800.0,  0.82, 0.81, 0.83, 500.0, 300.0),
            ('cid_resolved_win',  TIMESTAMP '2026-04-29 21:00:00', 5,  500.0,  0.80, 0.79, 0.81, 250.0, 250.0),
            ('cid_resolved_loss', TIMESTAMP '2026-04-29 23:00:00', 7,  700.0,  0.21, 0.20, 0.22, 200.0, 500.0),
            ('cid_active_1',      TIMESTAMP '2026-05-08 23:00:00', 5,  500.0,  0.89, 0.88, 0.90, 300.0, 200.0)
    """)

    # Wallet flow: один bucket для resolved_win
    con.execute("""
        CREATE TABLE wflow AS
        SELECT * FROM (VALUES
            ('cid_resolved_win', TIMESTAMP '2026-04-29 23:00:00', 5, 2, 200.0, 1, 50.0, 100.0, 8),
            ('cid_resolved_win', TIMESTAMP '2026-04-29 22:00:00', 4, 2, 150.0, 1, 45.0, 80.0, 6)
        ) AS t(condition_id, hour_bucket, n_unique_takers, n_unique_makers, whale_taker_volume, whale_taker_count, retail_taker_avg_size, whale_buy_pressure, taker_count_total)
    """)

    yield con
    con.close()


def test_canonical_resolved_only_returns_resolved_markets(synthetic_db):
    """Дефолтный market_filter='WHERE m.yes_won IS NOT NULL' — только resolved."""
    sql = build_features_sql(
        markets_src="markets",
        snaps_src="snaps",
        buckets_src="buckets",
        wflow_src="wflow",
        anchor_h=24,
        is_duckdb=True,  # canonical mode — обращаемся к tables как parquet sources не нужны
    )
    rows = synthetic_db.execute(sql).fetchall()
    cids = {r[0] for r in rows}
    assert cids == {"cid_resolved_win", "cid_resolved_loss"}


def test_live_filter_returns_active_only(synthetic_db):
    """Live filter возвращает только active markets, yes_won = NULL."""
    sql = build_features_sql(
        markets_src="markets",
        snaps_src="snaps",
        buckets_src="buckets",
        wflow_src="wflow",
        anchor_h=24,
        market_filter="WHERE m.yes_won IS NULL",  # active = yes_won not yet set
        is_duckdb=True,
    )
    rows = synthetic_db.execute(sql).fetchall()
    cids = {r[0] for r in rows}
    assert cids == {"cid_active_1", "cid_active_2"}
    # yes_won should be NULL для active
    yes_won_idx = FEATURE_COLUMNS.index("yes_won")
    for r in rows:
        assert r[yes_won_idx] is None


def test_schema_matches_feature_columns_list(synthetic_db):
    """SQL выдаёт колонки в порядке FEATURE_COLUMNS."""
    sql = build_features_sql(
        markets_src="markets",
        snaps_src="snaps",
        buckets_src="buckets",
        wflow_src="wflow",
        anchor_h=24,
        is_duckdb=True,
    )
    desc = synthetic_db.execute(sql).description
    actual_cols = [d[0] for d in desc]
    assert actual_cols == FEATURE_COLUMNS, (
        f"Schema mismatch:\n  Expected: {FEATURE_COLUMNS}\n  Actual:   {actual_cols}"
    )


def test_anchor_hours_set_correctly(synthetic_db):
    """anchor_hours в каждом row равен переданному параметру."""
    for h in [4, 24, 72, 168]:
        sql = build_features_sql(
            markets_src="markets",
            snaps_src="snaps",
            buckets_src="buckets",
            wflow_src="wflow",
            anchor_h=h,
            is_duckdb=True,
        )
        rows = synthetic_db.execute(sql).fetchall()
        anchor_idx = FEATURE_COLUMNS.index("anchor_hours")
        for r in rows:
            assert r[anchor_idx] == h


def test_yes_price_at_anchor_from_snapshot(synthetic_db):
    """yes_price_at_anchor берётся из snaps WHERE hours_to_resolve = anchor_h."""
    sql = build_features_sql(
        markets_src="markets",
        snaps_src="snaps",
        buckets_src="buckets",
        wflow_src="wflow",
        anchor_h=24,
        is_duckdb=True,
    )
    rows = synthetic_db.execute(sql).fetchall()
    cid_idx = FEATURE_COLUMNS.index("condition_id")
    price_idx = FEATURE_COLUMNS.index("yes_price_at_anchor")
    by_cid = {r[cid_idx]: float(r[price_idx]) for r in rows}
    assert by_cid["cid_resolved_win"] == 0.85
    assert by_cid["cid_resolved_loss"] == 0.20


def test_volume_asof_aggregates_pre_anchor_buckets(synthetic_db):
    """volume_asof = SUM(b.total_volume_usd) WHERE b.hour_bucket <= end_date - anchor."""
    sql = build_features_sql(
        markets_src="markets",
        snaps_src="snaps",
        buckets_src="buckets",
        wflow_src="wflow",
        anchor_h=24,
        is_duckdb=True,
    )
    rows = synthetic_db.execute(sql).fetchall()
    cid_idx = FEATURE_COLUMNS.index("condition_id")
    vol_idx = FEATURE_COLUMNS.index("volume_asof")
    by_cid = {r[cid_idx]: r[vol_idx] for r in rows}
    # cid_resolved_win has 3 buckets: 1000+800+500 = 2300
    assert float(by_cid["cid_resolved_win"]) == 2300.0
    # cid_resolved_loss has 1 bucket: 700
    assert float(by_cid["cid_resolved_loss"]) == 700.0


def test_event_arb_distance_computed_from_anchor_prices(synthetic_db):
    """event_arb_distance_yes = ABS(SUM(yes_price) - 1.0) для markets в event."""
    sql = build_features_sql(
        markets_src="markets",
        snaps_src="snaps",
        buckets_src="buckets",
        wflow_src="wflow",
        anchor_h=24,
        is_duckdb=True,
    )
    rows = synthetic_db.execute(sql).fetchall()
    cid_idx = FEATURE_COLUMNS.index("condition_id")
    arb_idx = FEATURE_COLUMNS.index("event_arb_distance_yes")
    by_cid = {r[cid_idx]: r[arb_idx] for r in rows}
    # evt_a: cid_resolved_win (0.85) + cid_resolved_loss (0.20) = 1.05; ABS(1.05-1) = 0.05
    assert abs(float(by_cid["cid_resolved_win"]) - 0.05) < 1e-9
    assert abs(float(by_cid["cid_resolved_loss"]) - 0.05) < 1e-9


def test_no_post_anchor_leakage(synthetic_db):
    """Buckets PO anchor_ts не должны влиять на aggregate."""
    # Добавим post-anchor bucket для cid_resolved_win
    synthetic_db.execute("""
        INSERT INTO buckets VALUES
        ('cid_resolved_win', TIMESTAMP '2026-05-01 01:00:00', 99, 9999.0, 0.99, 0.98, 1.0, 5000.0, 4000.0)
    """)
    sql = build_features_sql(
        markets_src="markets",
        snaps_src="snaps",
        buckets_src="buckets",
        wflow_src="wflow",
        anchor_h=24,
        is_duckdb=True,
    )
    rows = synthetic_db.execute(sql).fetchall()
    cid_idx = FEATURE_COLUMNS.index("condition_id")
    vol_idx = FEATURE_COLUMNS.index("volume_asof")
    by_cid = {r[cid_idx]: r[vol_idx] for r in rows}
    # volume_asof должен быть всё те же 2300 (post-anchor bucket исключён)
    assert float(by_cid["cid_resolved_win"]) == 2300.0


def test_neg_risk_cast_to_int(synthetic_db):
    """neg_risk: bool → int 0/1."""
    sql = build_features_sql(
        markets_src="markets",
        snaps_src="snaps",
        buckets_src="buckets",
        wflow_src="wflow",
        anchor_h=24,
        market_filter="WHERE 1=1",  # все markets
        is_duckdb=True,
    )
    rows = synthetic_db.execute(sql).fetchall()
    cid_idx = FEATURE_COLUMNS.index("condition_id")
    nr_idx = FEATURE_COLUMNS.index("neg_risk")
    by_cid = {r[cid_idx]: r[nr_idx] for r in rows}
    assert by_cid["cid_resolved_win"] == 0
    assert by_cid["cid_active_1"] == 1


def test_wallet_flow_zero_for_market_without_wflow_data(synthetic_db):
    """COALESCE(wf.*, 0) — market без wflow rows получает нули, не NULL."""
    sql = build_features_sql(
        markets_src="markets",
        snaps_src="snaps",
        buckets_src="buckets",
        wflow_src="wflow",
        anchor_h=24,
        is_duckdb=True,
    )
    rows = synthetic_db.execute(sql).fetchall()
    cid_idx = FEATURE_COLUMNS.index("condition_id")
    nut_idx = FEATURE_COLUMNS.index("n_unique_takers_24h")
    whale_idx = FEATURE_COLUMNS.index("whale_taker_volume_24h")
    by_cid = {r[cid_idx]: (r[nut_idx], r[whale_idx]) for r in rows}
    # cid_resolved_loss has no wflow rows → должны быть нули
    assert by_cid["cid_resolved_loss"] == (0, 0.0)
