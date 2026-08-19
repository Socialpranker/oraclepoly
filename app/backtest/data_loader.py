"""Canonical layer data loader — pandas-based, read-only.

Loads markets_clean + price_snapshots, applies filters, returns sorted iterator.

Memory budget: full markets (34 MB) + filtered snapshots (subset of 53 MB).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

from app.backtest.schema import BacktestConfig

logger = logging.getLogger(__name__)


def _condition_id_filter(condition_ids: set[str] | None) -> list[tuple[str, str, list[str]]] | None:
    """pyarrow predicate-pushdown фильтр по condition_id для read_parquet(filters=).

    None → читать весь файл (back-compat: tests/full-mode). Непустой set →
    [("condition_id", "in", [...])] — pyarrow пропускает row-groups без этих
    markets, не разворачивая весь parquet в RAM. Пустой set → фильтр, который
    не матчит ничего (корректнее, чем читать весь файл при заведомо нулевом
    пересечении).
    """
    if condition_ids is None:
        return None
    return [("condition_id", "in", list(condition_ids))]


class CanonicalDataLoader:
    """Lazy-loads canonical parquet files. Каждый instance кеширует frames in-memory."""

    def __init__(self, canonical_path: Path) -> None:
        self.canonical_path = canonical_path
        self._markets: pd.DataFrame | None = None
        self._snapshots: pd.DataFrame | None = None
        self._trade_buckets: pd.DataFrame | None = None
        self._buckets_by_market: dict[str, list[dict]] | None = None
        self._wallet_score: pd.DataFrame | None = None
        self._wallet_by_market: dict[str, dict[int, dict]] | None = None

    def reset_feature_caches(self) -> None:
        """Сбросить per-detector кэши, зависящие от condition_ids set.

        markets/snapshots — общие для всех детекторов (дёшево, держим в кэше).
        А trade_buckets/wallet кэшируются по ПЕРВОМУ набору condition_ids
        (`_buckets_by_market`/`_wallet_by_market`). При reuse одного loader для
        детекторов с РАЗНЫМИ фильтрами (например d14=crypto, прочие=all-cat)
        старый bucket-кэш молча отдаст обрезанный subset → неверные фичи.
        Bulk-runner зовёт это между детекторами. Pushdown (read_parquet filters)
        делает повторную загрузку buckets под новый фильтр дешёвой.
        """
        self._trade_buckets = None
        self._buckets_by_market = None
        self._wallet_score = None
        self._wallet_by_market = None

    def load_markets(self) -> pd.DataFrame:
        """Загрузить markets_clean.parquet (~34 MB, 427K rows)."""
        if self._markets is None:
            import pandas as pd

            path = self.canonical_path / "markets_clean.parquet"
            if not path.exists():
                raise FileNotFoundError(f"canonical markets not found: {path}")
            self._markets = pd.read_parquet(path)
            logger.info(
                "canonical_markets_loaded",
                extra={"path": str(path), "rows": len(self._markets)},
            )
        return self._markets

    def load_trade_buckets(self, condition_ids: set[str] | None = None) -> pd.DataFrame:
        """Загрузить trade_buckets.parquet (~501 MB) — для feature engineering.

        Конвертирует hour_bucket → bucket_ts_unix (int seconds).

        Args:
            condition_ids: если задан — predicate pushdown на уровне parquet
                (pyarrow filters): читаются ТОЛЬКО row-groups с этими markets,
                а не весь 501 MB файл в RAM. Снижает пик памяти в разы (15.5M
                строк × 126 row-groups → только релевантные). Без него весь
                файл разворачивается в pandas → ~13 GB RSS → OOM на 15 GB VPS.
                Кеш привязан к первому набору ids (backtest знает markets заранее).
        """
        if self._trade_buckets is None:
            import pandas as pd

            path = self.canonical_path / "trade_buckets.parquet"
            if not path.exists():
                raise FileNotFoundError(f"canonical trade_buckets not found: {path}")
            filters = _condition_id_filter(condition_ids)
            df = pd.read_parquet(path, filters=filters)
            # hour_bucket → bucket_ts_unix (epoch seconds), разрешение-независимо.
            # ⚠️ НЕ хардкодить делитель: pandas 2.x пишет parquet как ns, pandas 3.x
            # как us → astype("int64") даёт разный масштаб (был баг 10^6 vs 10^9 в CI).
            # Нормализуем tz (naive→UTC) и форсим ns ДО astype, потом ns→s.
            ts_ns = pd.to_datetime(df["hour_bucket"], utc=True).astype("datetime64[ns, UTC]")
            df["bucket_ts_unix"] = (ts_ns.astype("int64") // 10**9).astype(int)
            self._trade_buckets = df
            logger.info(
                "canonical_trade_buckets_loaded",
                extra={"path": str(path), "rows": len(df), "pushdown": filters is not None},
            )
        return self._trade_buckets

    def trade_buckets_by_market(
        self,
        condition_ids: set[str] | None = None,
    ) -> dict[str, list[dict]]:
        """Build per-market dict для O(1) lookup в ReplayEngine.

        Args:
            condition_ids: if set, читает только эти markets через parquet
                pushdown (память O(N×M), не весь файл). Если None — все markets
                (~3-5 GB → может OOM на маленьких VPS). Backtest всегда знает
                relevant markets заранее (filtered_markets).
        """
        if self._buckets_by_market is None:
            # Pushdown в read_parquet: фильтр по condition_id применяется при
            # чтении, поэтому в pandas попадает только нужный subset (а не isin
            # уже после полной загрузки 501 MB).
            df = self.load_trade_buckets(condition_ids=condition_ids)
            if condition_ids is not None:
                logger.info(
                    "trade_buckets_filtered_by_market_set",
                    extra={
                        "input_markets": len(condition_ids),
                        "matched_rows": len(df),
                    },
                )
            df_sorted = df.sort_values(["condition_id", "bucket_ts_unix"])
            buckets_map: dict[str, list[dict]] = {}
            for cid, group in df_sorted.groupby("condition_id", sort=False):
                buckets_map[str(cid)] = group.to_dict(orient="records")
            self._buckets_by_market = buckets_map
            logger.info(
                "trade_buckets_by_market_indexed",
                extra={"markets": len(buckets_map)},
            )
        return self._buckets_by_market

    def load_wallet_score(self, condition_ids: set[str] | None = None) -> pd.DataFrame | None:
        """Загрузить wallet_score_at_anchor.parquet (~50 MB, key (condition_id, anchor_hours)).

        Возвращает None если файл отсутствует — backtest деградирует, detect() сам skip
        на None wallet-полях.

        Args:
            condition_ids: если задан — predicate pushdown по condition_id при
                чтении parquet (как trade_buckets). Файл меньше (~50 MB), но
                фильтр единообразен и дешёв.
        """
        if self._wallet_score is None:
            import pandas as pd

            path = self.canonical_path / "wallet_score_at_anchor.parquet"
            if not path.exists():
                logger.warning("canonical_wallet_score_missing", extra={"path": str(path)})
                return None
            filters = _condition_id_filter(condition_ids)
            self._wallet_score = pd.read_parquet(path, filters=filters)
            logger.info(
                "canonical_wallet_score_loaded",
                extra={"path": str(path), "rows": len(self._wallet_score), "pushdown": filters is not None},
            )
        return self._wallet_score

    def wallet_score_by_market(
        self,
        condition_ids: set[str] | None = None,
    ) -> dict[str, dict[int, dict]]:
        """Build per-market dict: condition_id -> {anchor_hours -> row dict}.

        Возвращает {} если parquet отсутствует.
        """
        if self._wallet_by_market is None:
            df = self.load_wallet_score(condition_ids=condition_ids)
            if df is None:
                self._wallet_by_market = {}
                return self._wallet_by_market
            wmap: dict[str, dict[int, dict]] = {}
            for row in df.to_dict(orient="records"):
                cid = str(row["condition_id"])
                anchor = int(row["anchor_hours"])
                wmap.setdefault(cid, {})[anchor] = row
            self._wallet_by_market = wmap
            logger.info("wallet_score_by_market_indexed", extra={"markets": len(wmap)})
        return self._wallet_by_market

    def load_snapshots(self) -> pd.DataFrame:
        """Загрузить price_snapshots.parquet (~53 MB, 1.5M rows)."""
        if self._snapshots is None:
            import pandas as pd

            path = self.canonical_path / "price_snapshots.parquet"
            if not path.exists():
                raise FileNotFoundError(f"canonical snapshots not found: {path}")
            self._snapshots = pd.read_parquet(path)
            logger.info(
                "canonical_snapshots_loaded",
                extra={"path": str(path), "rows": len(self._snapshots)},
            )
        return self._snapshots

    def filtered_markets(self, config: BacktestConfig) -> pd.DataFrame:
        """Markets отфильтрованные по date range / category / max_markets."""
        markets = self.load_markets()
        # markets.end_date — pd timestamp (us). Конвертируем config tz-aware в pd-friendly.
        start_naive = config.start_date.astimezone(UTC).replace(tzinfo=None)
        end_naive = config.end_date.astimezone(UTC).replace(tzinfo=None)

        mask = (markets["end_date"] >= start_naive) & (markets["end_date"] < end_naive)
        if config.category_filter is not None:
            # case-insensitive: canonical имеет 'Sports'/'Crypto', filter — 'sports'/'crypto'
            lower_filter = {c.lower() for c in config.category_filter}
            mask &= markets["category"].str.lower().isin(list(lower_filter))

        filtered = markets.loc[mask].copy()
        if config.max_markets is not None and len(filtered) > config.max_markets:
            filtered = filtered.head(config.max_markets)
        logger.info(
            "markets_filtered",
            extra={
                "original": len(markets),
                "filtered": len(filtered),
                "start": str(config.start_date),
                "end": str(config.end_date),
            },
        )
        return filtered

    def iter_market_snapshots(
        self,
        markets: pd.DataFrame,
        tick_stride: int = 1,
    ) -> Iterator[tuple[dict, list[dict]]]:
        """Per market: yield (market_row, list_of_snapshot_rows).

        snapshots отсортированы по hours_to_resolve DESC (от far до near).
        tick_stride > 1 → каждый N-й snapshot.
        """
        snapshots = self.load_snapshots()
        # Build lookup по condition_id
        snap_by_market = snapshots.groupby("condition_id")
        market_dicts = markets.to_dict(orient="records")
        for mr in market_dicts:
            cid = mr["condition_id"]
            try:
                group = snap_by_market.get_group(cid)
            except KeyError:
                continue
            # Sort by hours_to_resolve DESC (replay from far to near resolve)
            group_sorted = group.sort_values("hours_to_resolve", ascending=False)
            if tick_stride > 1:
                group_sorted = group_sorted.iloc[::tick_stride]
            snapshot_dicts = group_sorted.to_dict(orient="records")
            yield mr, snapshot_dicts


def utc_dt(year: int, month: int, day: int) -> datetime:
    """Helper: tz-aware UTC datetime."""
    return datetime(year, month, day, tzinfo=UTC)
