"""Tests для predicate-pushdown в CanonicalDataLoader (фикс OOM 2026-06-14).

trade_buckets.parquet (501 MB) раньше читался целиком в pandas до фильтрации
→ ~13 GB RSS → OOM. Теперь read_parquet(filters=...) фильтрует по condition_id
на уровне parquet. Тесты проверяют: (1) pushdown даёт ТОТ ЖЕ результат, что
полное чтение + фильтрация, (2) edge cases (пустой/None set).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.backtest.data_loader import CanonicalDataLoader, _condition_id_filter


def _write_trade_buckets(tmp_path: Path) -> Path:
    """3 markets × 2 bucket'а каждый. hour_bucket — tz-aware us timestamp."""
    df = pd.DataFrame(
        [
            {"condition_id": "0xaaa", "hour_bucket": pd.Timestamp("2026-01-01T00:00:00Z"),
             "n_trades": 10, "total_volume_usd": 100.0, "vwap_yes": 0.5,
             "min_yes_price": 0.4, "max_yes_price": 0.6, "buy_volume": 60.0,
             "sell_volume": 40.0, "n_distinct_makers": 3, "n_distinct_takers": 5},
            {"condition_id": "0xaaa", "hour_bucket": pd.Timestamp("2026-01-01T01:00:00Z"),
             "n_trades": 20, "total_volume_usd": 200.0, "vwap_yes": 0.55,
             "min_yes_price": 0.5, "max_yes_price": 0.6, "buy_volume": 120.0,
             "sell_volume": 80.0, "n_distinct_makers": 4, "n_distinct_takers": 8},
            {"condition_id": "0xbbb", "hour_bucket": pd.Timestamp("2026-01-02T00:00:00Z"),
             "n_trades": 5, "total_volume_usd": 50.0, "vwap_yes": 0.3,
             "min_yes_price": 0.25, "max_yes_price": 0.35, "buy_volume": 20.0,
             "sell_volume": 30.0, "n_distinct_makers": 2, "n_distinct_takers": 3},
            {"condition_id": "0xccc", "hour_bucket": pd.Timestamp("2026-01-03T00:00:00Z"),
             "n_trades": 8, "total_volume_usd": 80.0, "vwap_yes": 0.7,
             "min_yes_price": 0.65, "max_yes_price": 0.75, "buy_volume": 50.0,
             "sell_volume": 30.0, "n_distinct_makers": 3, "n_distinct_takers": 4},
        ]
    )
    path = tmp_path / "trade_buckets.parquet"
    df.to_parquet(path)
    return tmp_path


# --- helper _condition_id_filter ---------------------------------------------

def test_filter_none_returns_none():
    """None → читать весь файл (back-compat)."""
    assert _condition_id_filter(None) is None


def test_filter_set_builds_in_predicate():
    f = _condition_id_filter({"0xaaa", "0xbbb"})
    assert f is not None
    assert len(f) == 1
    col, op, vals = f[0]
    assert col == "condition_id"
    assert op == "in"
    assert set(vals) == {"0xaaa", "0xbbb"}


def test_filter_empty_set_matches_nothing():
    """Пустой set → фильтр с пустым списком (ничего не матчит), не None."""
    f = _condition_id_filter(set())
    assert f == [("condition_id", "in", [])]


# --- trade_buckets pushdown ---------------------------------------------------

def test_trade_buckets_pushdown_filters_to_subset(tmp_path):
    canonical = _write_trade_buckets(tmp_path)
    loader = CanonicalDataLoader(canonical)
    buckets = loader.trade_buckets_by_market(condition_ids={"0xaaa"})

    assert set(buckets.keys()) == {"0xaaa"}
    assert len(buckets["0xaaa"]) == 2
    # bucket_ts_unix вычислен (us → s)
    ts = [b["bucket_ts_unix"] for b in buckets["0xaaa"]]
    assert ts == sorted(ts)  # отсортировано по времени
    assert ts[0] == int(pd.Timestamp("2026-01-01T00:00:00Z").value // 10**9)


def test_trade_buckets_pushdown_equals_full_read(tmp_path):
    """Главная гарантия: pushdown-результат идентичен read-all + ручной фильтр."""
    canonical = _write_trade_buckets(tmp_path)
    subset = {"0xaaa", "0xccc"}

    # Через pushdown
    loader_pd = CanonicalDataLoader(canonical)
    via_pushdown = loader_pd.trade_buckets_by_market(condition_ids=subset)

    # Эталон: читаем всё, фильтруем вручную в pandas (старое поведение)
    df_all = pd.read_parquet(canonical / "trade_buckets.parquet")
    df_all["bucket_ts_unix"] = (df_all["hour_bucket"].astype("int64") // 10**6).astype(int)
    df_all = df_all[df_all["condition_id"].isin(subset)].sort_values(
        ["condition_id", "bucket_ts_unix"]
    )
    expected: dict[str, int] = df_all.groupby("condition_id").size().to_dict()

    assert set(via_pushdown.keys()) == set(expected.keys())
    for cid, n in expected.items():
        assert len(via_pushdown[cid]) == n


def test_trade_buckets_no_filter_reads_all(tmp_path):
    """condition_ids=None → все markets (back-compat для full-mode/tests)."""
    canonical = _write_trade_buckets(tmp_path)
    loader = CanonicalDataLoader(canonical)
    buckets = loader.trade_buckets_by_market(condition_ids=None)
    assert set(buckets.keys()) == {"0xaaa", "0xbbb", "0xccc"}


# --- reset_feature_caches (bulk reuse одного loader для разных фильтров) -------

def test_reset_feature_caches_reloads_under_new_filter(tmp_path):
    """Кэш buckets привязан к первому condition_ids set; reset → перезагрузка под новый.

    Регрессия на bulk-ловушку: d14=crypto-subset закэшировал бы обрезанные buckets,
    последующие all-cat детекторы получили бы subset. reset_feature_caches лечит.
    """
    canonical = _write_trade_buckets(tmp_path)
    loader = CanonicalDataLoader(canonical)

    # 1-й детектор: узкий фильтр
    narrow = loader.trade_buckets_by_market(condition_ids={"0xaaa"})
    assert set(narrow.keys()) == {"0xaaa"}

    # Без reset — закэшированный subset вернётся даже под другим фильтром
    stale = loader.trade_buckets_by_market(condition_ids={"0xbbb", "0xccc"})
    assert set(stale.keys()) == {"0xaaa"}, "без reset кэш отдаёт старый subset"

    # После reset — перезагрузка под новый фильтр
    loader.reset_feature_caches()
    fresh = loader.trade_buckets_by_market(condition_ids={"0xbbb", "0xccc"})
    assert set(fresh.keys()) == {"0xbbb", "0xccc"}


def test_reset_feature_caches_keeps_markets_snapshots(tmp_path):
    """reset чистит ТОЛЬКО feature-кэши, дорогие markets/snapshots остаются."""
    import pandas as pd

    # markets + snapshots, чтобы было что сохранять
    pd.DataFrame([{"condition_id": "0xaaa", "end_date": pd.Timestamp("2026-01-01"),
                   "category": "Crypto", "yes_won": 1, "question": "q"}]).to_parquet(
        tmp_path / "markets_clean.parquet")
    pd.DataFrame([{"condition_id": "0xaaa", "hours_to_resolve": 24, "yes_price": 0.5,
                   "trade_ts": 1}]).to_parquet(tmp_path / "price_snapshots.parquet")

    loader = CanonicalDataLoader(tmp_path)
    loader.load_markets()
    loader.load_snapshots()
    assert loader._markets is not None
    assert loader._snapshots is not None

    loader.reset_feature_caches()
    assert loader._markets is not None, "markets НЕ должны сбрасываться"
    assert loader._snapshots is not None, "snapshots НЕ должны сбрасываться"
    assert loader._buckets_by_market is None
    assert loader._wallet_by_market is None
