"""Historical PIT Loader — point-in-time oracle score для исторических рынков.

Задача исследования: bin-level анализ edge oracle_score (Вариант C).
Журнал: docs/research/2026-04-10-oracle-pit-findings.md

Что делает:
1. Читает market_enriched + market_aggregates из /mnt/data/polymarket_dataset/ (VPS)
2. Для каждого resolved market выбирает 3 PIT точки (early / mid / late)
3. Через HTTPS streaming читает trades из quant.parquet (568M строк, HuggingFace)
   с фильтром по condition_id (row-group pruning → секунды на market)
4. На каждой PIT точке считает pit_market_price, maker_direction_raw, taker_imbalance_raw,
   whale_raw (прокси через whale_volume fraction)
5. Прогоняет через настоящий BayesianScoreCalculator.calculate_oracle_score()
   ДВАЖДЫ — с прод-весами из calibration_configs И с DEFAULT_WEIGHTS (research)
6. Записывает инкрементально в /mnt/data/polymarket_dataset/pit_dataset.parquet

Почему два набора весов: см. журнал, раздел "Прод БД содержит ДРУГИЕ веса".
Прод отошёл от research-рекомендаций, мы сравним, какой вариант даёт больше edge.

Запуск на VPS:
    cd ~/oracle
    source .venv/bin/activate  # или venv path
    nohup python scripts/research/historical_pit_loader.py --limit 5000 > /tmp/pit_loader.log 2>&1 &
    tail -f /tmp/pit_loader.log

Параметры:
    --limit N          Максимум markets (для smoke-test). Default: 5000.
    --min-volume USD   Нижний порог total_volume_usd. Default: 10000.
    --out PATH         Выходной parquet. Default: /mnt/data/polymarket_dataset/pit_dataset.parquet
    --calibration PATH JSON снимка прод-калибровки. Default:
                       docs/research/2026-04-10-prod-calibration-snapshot.json
"""

import argparse
import datetime as dt
import json
import os
import sys
import time

import duckdb

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.core.scoring import BayesianScoreCalculator  # noqa: E402

# ────────────────────────────────────────────────────────────────────────────
# Константы
# ────────────────────────────────────────────────────────────────────────────

DATA_DIR = "/mnt/data/polymarket_dataset"
ENRICHED = f"{DATA_DIR}/market_enriched.parquet"
AGGREGATES = f"{DATA_DIR}/market_aggregates.parquet"

# Local quant.parquet (скачан с HuggingFace, 36 GB, ~568M trades).
# Fallback: HTTPS streaming, но в 10× медленнее. Loader автоматически переключается.
QUANT_LOCAL = "/root/hf_data/quant.parquet"
QUANT_URL = "https://huggingface.co/datasets/SII-WANGZJ/Polymarket_data/resolve/main/quant.parquet"

DEFAULT_OUT = f"{DATA_DIR}/pit_dataset.parquet"
DEFAULT_CALIBRATION = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "docs/research/2026-04-10-prod-calibration-snapshot.json",
)

# Окно последней сделки для pit_market_price (если давности > этого — точка скипается)
MAX_PRICE_STALENESS_SEC = 24 * 3600  # 24 часа

# Окно для maker/taker imbalance (аналог прод-логики, TTL 2h)
WINDOW_SEC = 2 * 3600  # 2 часа до T

# Пороги для выбора 3-х PIT точек
PIT_EARLY_OFFSET_SEC = 24 * 3600  # +24h после created_at
PIT_LATE_OFFSET_SEC = 3 * 24 * 3600  # -3d до end_date


# ────────────────────────────────────────────────────────────────────────────
# Калибровка: загрузка и применение
# ────────────────────────────────────────────────────────────────────────────

def load_calibration(path: str) -> dict[str, dict]:
    """Загрузить прод-калибровку per category (+ global fallback)."""
    with open(path) as f:
        rows = json.load(f)
    by_cat: dict[str, dict] = {}
    for row in rows:
        cat = row["category"]
        by_cat[cat] = row
    if "global" not in by_cat:
        raise ValueError("Снимок калибровки не содержит global fallback")
    return by_cat


def calibration_for_category(by_cat: dict, category: str | None) -> dict:
    """Подобрать калибровку: точная категория → global fallback."""
    if category and category in by_cat:
        return by_cat[category]
    return by_cat["global"]


def weights_from_calibration(calib: dict) -> dict[str, float]:
    """Конвертировать row calibration_configs → dict weights для BayesianScoreCalculator."""
    return {
        "w_mkt": float(calib["w_mkt"]),
        "w_whale": float(calib["w_whale"]),
        "w_whale_contra": float(calib["w_whale_contra"]),
        "w_fresh": float(calib.get("w_fresh") or 0.0),
        "w_cluster": float(calib.get("w_cluster") or 0.0),
        "w_registry": float(calib.get("w_registry") or 0.0),
        "w_oi": float(calib.get("w_oi") or 0.0),
        "w_concentration": float(calib.get("w_concentration") or 0.0),
        "w_micro": float(calib.get("w_micro") or 0.0),
        "w_retail": float(calib.get("w_retail") or 0.0),
        "w_maker_direction": float(calib.get("w_maker_direction") or 0.0),
        "w_taker_imbalance": float(calib.get("w_taker_imbalance") or 0.0),
        "w_sports_odds": 0.5,  # отсутствует в снимке, берём default
    }


# ────────────────────────────────────────────────────────────────────────────
# Proxy факторы из raw trades
# ────────────────────────────────────────────────────────────────────────────

def whale_raw_from_fraction(whale_volume: float, total_volume: float) -> float:
    """Proxy Λ_whale из доли whale-оборота (аналог tournament-скрипта).

    Это НЕ настоящий whale_bayes_factor (нет Kelly/credibility/liquidity gate).
    Честно фиксируем в отчёте как ограничение: PIT-loader использует volume-proxy.
    """
    if not total_volume or total_volume <= 0:
        return 0.0
    fraction = whale_volume / total_volume
    # Центрируем на 15% (baseline whale share), масштабируем, clamp на ±0.3
    raw = (fraction - 0.15) * 2.0
    return max(-0.3, min(0.3, raw))


def maker_direction_raw(buy_usd: float, sell_usd: float) -> float:
    """Λ_maker аналог maker_direction_bayes_factor (scoring.py:773-794).

    Формула: λ = 0.8 × ((buy - sell) / (buy + sell)), clamp ±0.8.
    В проде `buy`/`sell` — это bid/ask, у нас — buy_usd/sell_usd maker-side.
    Смысл тот же: дисбаланс лимитных ордеров.
    """
    total = (buy_usd or 0) + (sell_usd or 0)
    if total <= 0:
        return 0.0
    ratio = (buy_usd - sell_usd) / total
    return max(-0.8, min(0.8, 0.8 * ratio))


def taker_imbalance_raw(buy_usd: float, sell_usd: float) -> float:
    """Λ_taker: (buy - sell) / (buy + sell), scaled."""
    total = (buy_usd or 0) + (sell_usd or 0)
    if total <= 0:
        return 0.0
    return (buy_usd - sell_usd) / total


# ────────────────────────────────────────────────────────────────────────────
# PIT точки: для заданного market получить 3 момента
# ────────────────────────────────────────────────────────────────────────────

def choose_pit_timestamps(created_at: dt.datetime, end_date: dt.datetime) -> list[tuple[str, int]]:
    """Вернуть 3 PIT точки: [(phase, unix_ts), ...]."""
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=dt.UTC)
    if end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=dt.UTC)

    life_sec = (end_date - created_at).total_seconds()
    if life_sec <= PIT_EARLY_OFFSET_SEC + PIT_LATE_OFFSET_SEC:
        # Короткий рынок — берём только mid
        mid = created_at + dt.timedelta(seconds=life_sec / 2)
        return [("mid", int(mid.timestamp()))]

    early = created_at + dt.timedelta(seconds=PIT_EARLY_OFFSET_SEC)
    mid = created_at + dt.timedelta(seconds=life_sec / 2)
    late = end_date - dt.timedelta(seconds=PIT_LATE_OFFSET_SEC)
    return [
        ("early", int(early.timestamp())),
        ("mid", int(mid.timestamp())),
        ("late", int(late.timestamp())),
    ]


# ────────────────────────────────────────────────────────────────────────────
# HTTPS streaming: агрегаты для market+time_point
# ────────────────────────────────────────────────────────────────────────────

def fetch_market_trades(
    con: duckdb.DuckDBPyConnection, condition_id: str, quant_path: str,
) -> list[tuple]:
    """Загрузить ВСЕ trades для одного market за один запрос.

    Возвращает (timestamp, price, usd_amount, side, taker) — taker добавлен
    для подсчёта unique_takers в окне.
    """
    rows = con.execute(
        f"""
        SELECT timestamp, price, usd_amount, side, taker
        FROM read_parquet('{quant_path}')
        WHERE condition_id = '{condition_id}'
        ORDER BY timestamp
        """
    ).fetchall()
    return rows


def fetch_all_trades_bulk(
    con: duckdb.DuckDBPyConnection,
    condition_ids: list[str],
    quant_path: str,
    chunk_size: int = 200,
) -> dict[str, list[tuple]]:
    """Загрузить trades для ВСЕХ markets чанками (защита от OOM).

    Старая версия делала один SELECT WHERE condition_id IN (10K markets) и
    .fetchall() в Python list — на VPS с малым объёмом RAM это вызывало OOM kill.

    Новая версия:
    - Разбивает condition_ids на чанки по `chunk_size` (default 200)
    - Каждый чанк — отдельный SELECT + fetch_arrow_table() (Arrow вместо Python tuples)
    - Конвертит в Python tuples для совместимости с downstream
    - Освобождает Arrow buffer после каждого чанка через явный `del`
    - На 1000 markets: ~5 чанков × ~200 markets каждый, peak RAM ~1.5GB вместо 7GB

    Один последовательный проход по quant.parquet на каждый чанк, но DuckDB
    использует row-group pruning по condition_id → каждый scan быстрый
    (~5-15 сек на чанк против 30-60 сек на полный scan).

    Возвращает dict: condition_id → [(timestamp, price, usd_amount, side, taker), ...] sorted.
    """
    from collections import defaultdict

    result: dict[str, list[tuple]] = defaultdict(list)
    n_chunks = (len(condition_ids) + chunk_size - 1) // chunk_size
    total_rows = 0

    for chunk_idx in range(n_chunks):
        chunk = condition_ids[chunk_idx * chunk_size : (chunk_idx + 1) * chunk_size]
        cid_list = ", ".join(f"'{c}'" for c in chunk)

        # to_arrow_table() — memory-efficient: Arrow columnar buffer вместо
        # Python list of tuples (≈10× меньше memory overhead).
        # (fetch_arrow_table — deprecated alias, оставлен для backward-compat)
        result_obj = con.execute(
            f"""
            SELECT condition_id, timestamp, price, usd_amount, side, taker
            FROM read_parquet('{quant_path}')
            WHERE condition_id IN ({cid_list})
            ORDER BY condition_id, timestamp
            """
        )
        # Используем to_arrow_table() (DuckDB 1.0+), fallback на fetch_arrow_table
        # для старых версий
        if hasattr(result_obj, "to_arrow_table"):
            arrow_table = result_obj.to_arrow_table()
        else:
            arrow_table = result_obj.fetch_arrow_table()

        # Конвертация в Python tuples только нужных колонок per row
        cids = arrow_table.column("condition_id").to_pylist()
        timestamps = arrow_table.column("timestamp").to_pylist()
        prices = arrow_table.column("price").to_pylist()
        usds = arrow_table.column("usd_amount").to_pylist()
        sides = arrow_table.column("side").to_pylist()
        takers = arrow_table.column("taker").to_pylist()

        n_rows = len(cids)
        total_rows += n_rows
        for i in range(n_rows):
            result[cids[i]].append((timestamps[i], prices[i], usds[i], sides[i], takers[i]))

        # Явный release Arrow memory перед следующим чанком
        del arrow_table, cids, timestamps, prices, usds, sides, takers

        if (chunk_idx + 1) % 5 == 0 or chunk_idx == n_chunks - 1:
            print(
                f"      [chunk {chunk_idx + 1}/{n_chunks}] "
                f"loaded {total_rows:,} trades total"
            )

    return dict(result)


# Дополнительные временные константы для новых фич
WINDOW_24H_SEC = 24 * 3600
WINDOW_7D_SEC = 7 * 24 * 3600
FIRST_24H_SEC = 24 * 3600  # первые 24h после created_at


def compute_pit_from_trades(
    trades: list[tuple],
    t: int,
    created_at_ts: int,
) -> dict | None:
    """По отсортированному списку trades собрать PIT snapshot на момент t.

    trades: list[(timestamp, price, usd_amount, side, taker)] — sorted ASC.
    created_at_ts: unix timestamp момента создания рынка (для first_24h метрик).

    Считает все исходные фичи (для oracle score) + новые exploratory фичи:
      - price_24h_ago (для momentum)
      - price_7d_vwap (для mean reversion baseline)
      - price_std_first_24h (для early-flat гипотезы)
      - whale_buy_ratio_2h (для whale accumulation)
      - unique_takers_2h (для crowd concentration)
      - volume_last_2h_usd (для volume shock)
      - median_hourly_volume_7d (для volume shock normalization)
      - price_at_24h_mark (цена в конце первых 24h, нужна для std если её считать не
        в lifetime первых 24h, а в окне 24h до T — см. ниже)
    """
    if not trades:
        return None

    window_2h_lo = t - WINDOW_SEC
    window_7d_lo = t - WINDOW_7D_SEC
    first_24h_hi = created_at_ts + FIRST_24H_SEC

    last_price: float | None = None
    last_ts: int | None = None

    # Oracle-раньше существующие метрики
    taker_buy = 0.0
    taker_sell = 0.0
    vol_up_to_t = 0.0
    whale_vol_up_to_t = 0.0

    # Новые фичи
    # Momentum: нам нужна цена как можно ближе к t - 24h. Ищем последнюю сделку ≤ (t - 24h).
    price_at_24h_lo: float | None = None
    price_at_24h_lo_ts: int | None = None

    # VWAP 7d: sum(price * usd) / sum(usd) в окне [t-7d, t]
    vwap_7d_num = 0.0
    vwap_7d_den = 0.0

    # First 24h std — цены в окне [created_at, created_at + 24h]
    first_24h_prices: list[float] = []

    # Whale buy ratio в 2h окне
    whale_buy_2h_usd = 0.0
    whale_total_2h_usd = 0.0

    # Unique takers в 2h окне
    takers_2h: set[str] = set()

    # Volume в 2h окне
    vol_2h = 0.0

    # Per-hour volume для 7d окна (для median)
    hour_buckets: dict[int, float] = {}

    for row in trades:
        ts, price, usd, side, taker = row[0], row[1], row[2], row[3], row[4]

        if ts > t:
            break

        # === existing oracle metrics ===
        vol_up_to_t += usd
        if usd >= 1000:
            whale_vol_up_to_t += usd

        last_price = price
        last_ts = ts

        if ts >= window_2h_lo:
            vol_2h += usd
            if taker:
                takers_2h.add(taker)
            if usd >= 1000:
                whale_total_2h_usd += usd
                if side == "BUY":
                    whale_buy_2h_usd += usd
            if side == "BUY":
                taker_buy += usd
            elif side == "SELL":
                taker_sell += usd

        # === momentum: цена ≤ t-24h ===
        if ts <= t - WINDOW_24H_SEC:
            price_at_24h_lo = price
            price_at_24h_lo_ts = ts

        # === 7d VWAP ===
        if ts >= window_7d_lo:
            vwap_7d_num += price * usd
            vwap_7d_den += usd
            # Per-hour bucket для median
            bucket = ts // 3600
            hour_buckets[bucket] = hour_buckets.get(bucket, 0.0) + usd

        # === first 24h std ===
        if created_at_ts <= ts <= first_24h_hi:
            first_24h_prices.append(price)

    if last_price is None or last_ts is None:
        return None
    if t - last_ts > MAX_PRICE_STALENESS_SEC:
        return None

    # maker_buy / maker_sell — противоположны taker side
    maker_buy = taker_sell
    maker_sell = taker_buy

    # === Вычисляем финальные фичи ===

    # Momentum: разница цены сейчас и 24h назад. Если 24h назад не было сделок — None.
    # Price staleness для 24h-ago: допускаем разницу ≤ 6h от точки T-24h.
    momentum_24h: float | None = None
    if price_at_24h_lo is not None and price_at_24h_lo_ts is not None:
        age = (t - WINDOW_24H_SEC) - price_at_24h_lo_ts
        if age <= 6 * 3600:  # точка свежая
            momentum_24h = last_price - price_at_24h_lo

    # 7d VWAP
    vwap_7d = (vwap_7d_num / vwap_7d_den) if vwap_7d_den > 0 else None

    # First 24h std (нужно ≥5 сделок для meaningful std)
    if len(first_24h_prices) >= 5:
        import statistics
        std_first_24h = statistics.stdev(first_24h_prices)
    else:
        std_first_24h = None

    # Whale buy ratio
    whale_buy_ratio_2h = (whale_buy_2h_usd / whale_total_2h_usd) if whale_total_2h_usd > 0 else None

    # Median hourly volume за 7d
    if hour_buckets:
        import statistics
        median_hourly = statistics.median(hour_buckets.values())
    else:
        median_hourly = None

    return {
        # === исходные oracle фичи ===
        "market_price": float(last_price),
        "last_trade_ts": int(last_ts),
        "taker_buy_usd": taker_buy,
        "taker_sell_usd": taker_sell,
        "maker_buy_usd": maker_buy,
        "maker_sell_usd": maker_sell,
        "vol_up_to_t": vol_up_to_t,
        "whale_vol_up_to_t": whale_vol_up_to_t,
        # === новые exploratory фичи ===
        "momentum_24h": momentum_24h,  # price_now - price_24h_ago
        "price_24h_ago": price_at_24h_lo,
        "vwap_7d": vwap_7d,
        "std_first_24h": std_first_24h,
        "whale_buy_ratio_2h": whale_buy_ratio_2h,
        "unique_takers_2h": len(takers_2h),
        "volume_2h": vol_2h,
        "median_hourly_volume_7d": median_hourly,
    }


# ────────────────────────────────────────────────────────────────────────────
# Oracle score для snapshot (два набора весов)
# ────────────────────────────────────────────────────────────────────────────

def compute_oracle(
    calc: BayesianScoreCalculator,
    market_price: float,
    whale_raw: float,
    maker_raw: float,
    taker_raw: float,
    category: str | None,
    weights: dict[str, float],
    temperature: float,
    calibration_method: str,
    beta_a: float,
    beta_b: float,
) -> dict:
    """Вызвать настоящий calculate_oracle_score с явными весами.

    Обходит calc.weights и передаёт weights напрямую.
    """
    return calc.calculate_oracle_score(
        market_price=market_price,
        whale_factors=[whale_raw] if whale_raw != 0.0 else [],
        weights=weights,
        temperature=temperature,
        oi_factor=0.0,
        concentration_factor=0.0,
        micro_factor=0.0,  # deprecated (см. project_micro_factor_dead memory)
        sports_odds_factor=0.0,
        retail_factor=taker_raw,  # retail ≈ fade taker flow, прокси
        maker_direction_factor=maker_raw,
        taker_imbalance_factor=taker_raw,
        category=category.lower() if category else None,
        calibration_method=calibration_method,
        beta_a=beta_a,
        beta_b=beta_b,
    )


# ────────────────────────────────────────────────────────────────────────────
# Main loop
# ────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Historical PIT Loader — oracle score edge analysis")
    parser.add_argument("--limit", type=int, default=5000, help="Max markets (smoke-test)")
    parser.add_argument("--min-volume", type=float, default=10_000.0, help="Min total_volume_usd")
    parser.add_argument("--out", type=str, default=DEFAULT_OUT)
    parser.add_argument("--calibration", type=str, default=DEFAULT_CALIBRATION)
    parser.add_argument("--resume", action="store_true", help="Resume from existing pit_dataset")
    parser.add_argument("--quant", type=str, default=None,
                        help="Path to quant.parquet. Default: local if exists, else HTTPS.")
    args = parser.parse_args()

    # Выбор источника quant: локальный приоритет
    if args.quant:
        quant_path = args.quant
    elif os.path.exists(QUANT_LOCAL):
        quant_path = QUANT_LOCAL
        print(f"      using local {QUANT_LOCAL} (fast)")
    else:
        quant_path = QUANT_URL
        print(f"      using HTTPS streaming {QUANT_URL} (slow)")

    print("=" * 70)
    print("HISTORICAL PIT LOADER — Oracle Score Edge Analysis")
    print("=" * 70)

    # ── 1. Калибровка ──
    print(f"\n[1/6] Loading calibration from {args.calibration}")
    calib_by_cat = load_calibration(args.calibration)
    print(f"      {len(calib_by_cat)} category configs loaded: {sorted(calib_by_cat.keys())}")

    # ── 2. DuckDB connection ──
    print("\n[2/6] Setting up DuckDB + httpfs")
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("SET enable_http_metadata_cache=true;")
    con.execute("SET enable_object_cache=true;")
    # OOM protection: на небольшом VPS Docker отъедает заметную часть RAM.
    # DuckDB 2GB + Python 1.5-2GB peak (chunked fetch_arrow_table) = ~4GB total, safe margin.
    con.execute("SET memory_limit='2GB';")
    con.execute("SET threads=2;")
    con.execute("SET temp_directory='/tmp/duckdb';")
    os.makedirs("/tmp/duckdb", exist_ok=True)

    calc = BayesianScoreCalculator()
    research_weights = BayesianScoreCalculator.DEFAULT_WEIGHTS.copy()

    # ── 3. Список markets для обработки ──
    print("\n[3/6] Selecting markets")
    already_done: set[str] = set()
    if args.resume and os.path.exists(args.out):
        print(f"      resume from {args.out}")
        existing = con.execute(
            f"SELECT DISTINCT condition_id FROM read_parquet('{args.out}')"
        ).fetchall()
        already_done = {r[0] for r in existing}
        print(f"      {len(already_done):,} markets already done")

    markets_q = con.execute(f"""
        SELECT
            m.condition_id, m.category, m.winner, m.answer1, m.answer2,
            m.created_at, m.end_date, m.volume,
            a.total_volume_usd
        FROM read_parquet('{ENRICHED}') m
        JOIN read_parquet('{AGGREGATES}') a USING (condition_id)
        WHERE m.is_resolved = true
          AND m.winner IS NOT NULL
          AND a.total_volume_usd >= {args.min_volume}
          AND m.end_date IS NOT NULL
          AND m.created_at IS NOT NULL
        ORDER BY a.total_volume_usd DESC
        LIMIT {args.limit * 2}
    """).fetchall()

    markets = [m for m in markets_q if m[0] not in already_done][: args.limit]
    print(f"      {len(markets):,} markets selected (filter: total_volume ≥ ${args.min_volume:,.0f})")

    # ── 4. Bulk fetch trades — chunked для OOM защиты ──
    # На VPS 8GB top-markets имеют до 2.7M trades. При chunk_size=200 worst-case
    # ~5GB Python list → OOM kill. Используем chunk_size=20 + Arrow.
    # На больших --limit (>2000) — ещё агрессивнее (10) или per-market fetch.
    all_cids = [m[0] for m in markets]
    if len(all_cids) > 2000:
        # Per-market fetch — защита от OOM на крупных runs.
        # Память освобождается после каждого market'а (compute snap → drop trades).
        bulk_mode = "per_market"
    elif len(all_cids) > 500:
        bulk_mode = "chunk_20"
    else:
        bulk_mode = "chunk_200"

    print(f"\n[4/5] Loading trades for {len(all_cids):,} markets (mode={bulk_mode})")
    t_bulk = time.time()
    trades_by_cid: dict[str, list[tuple]] = {}
    if bulk_mode == "chunk_200":
        trades_by_cid = fetch_all_trades_bulk(con, all_cids, quant_path, chunk_size=200)
    elif bulk_mode == "chunk_20":
        trades_by_cid = fetch_all_trades_bulk(con, all_cids, quant_path, chunk_size=20)
    # bulk_mode == "per_market" — fetch внутри loop'а ниже
    bulk_elapsed = time.time() - t_bulk
    if trades_by_cid:
        total_trades = sum(len(v) for v in trades_by_cid.values())
        print(f"      Loaded {total_trades:,} trades in {bulk_elapsed:.1f}s "
              f"({len(trades_by_cid):,} markets with trades)")
    else:
        print(f"      Per-market mode: trades будут загружаться по одному")

    # ── 5. PIT loop ──
    print(f"\n[5/6] Computing PIT snapshots (3 points/market)")

    out_rows: list[dict] = []
    t0 = time.time()
    errors = 0
    skipped_stale = 0
    processed = 0

    for idx, m in enumerate(markets):
        (cid, category, winner, a1, a2, created, end_date, mkt_volume, total_vol) = m

        try:
            pit_points = choose_pit_timestamps(created, end_date)
        except Exception:
            errors += 1
            continue

        # Trades: либо из bulk dict, либо fetch on-demand (memory-safe)
        if bulk_mode == "per_market":
            trades = fetch_market_trades(con, cid, quant_path)
        else:
            trades = trades_by_cid.get(cid, [])

        if not trades:
            continue

        # created_at → unix timestamp (для first_24h метрик)
        if created.tzinfo is None:
            created_utc = created.replace(tzinfo=dt.UTC)
        else:
            created_utc = created
        created_at_ts = int(created_utc.timestamp())

        for phase, t in pit_points:
            snap = compute_pit_from_trades(trades, t, created_at_ts)

            if snap is None:
                skipped_stale += 1
                continue

            mp = snap["market_price"]
            if mp <= 0.01 or mp >= 0.99:
                continue  # boundary markets — нестабильные

            whale_r = whale_raw_from_fraction(snap["whale_vol_up_to_t"], snap["vol_up_to_t"])
            maker_r = maker_direction_raw(snap["maker_buy_usd"], snap["maker_sell_usd"])
            taker_r = taker_imbalance_raw(snap["taker_buy_usd"], snap["taker_sell_usd"])

            # Две версии oracle score
            prod_cal = calibration_for_category(calib_by_cat, category)
            prod_weights = weights_from_calibration(prod_cal)
            prod_T = float(prod_cal["temperature"])
            prod_method = prod_cal["calibration_method"]
            prod_ba = float(prod_cal["beta_a"])
            prod_bb = float(prod_cal["beta_b"])

            try:
                oracle_prod = compute_oracle(
                    calc, mp, whale_r, maker_r, taker_r, category,
                    prod_weights, prod_T, prod_method, prod_ba, prod_bb,
                )
                oracle_research = compute_oracle(
                    calc, mp, whale_r, maker_r, taker_r, category,
                    research_weights, 1.3, "temperature", 1.0, 0.0,
                )
            except Exception:
                errors += 1
                continue

            # Определяем side и outcome
            div_prod = oracle_prod["oracle_score"] - mp
            div_research = oracle_research["oracle_score"] - mp

            # Side — по знаку divergence
            side_prod = "YES" if div_prod >= 0 else "NO"
            side_research = "YES" if div_research >= 0 else "NO"

            # Outcome: winner == answer1 (YES) или answer2 (NO)
            is_yes_winner = (winner == a1)
            is_no_winner = (winner == a2)
            if not (is_yes_winner or is_no_winner):
                continue  # битый winner

            prod_win = (side_prod == "YES" and is_yes_winner) or (side_prod == "NO" and is_no_winner)
            research_win = (side_research == "YES" and is_yes_winner) or (side_research == "NO" and is_no_winner)

            # PnL per $1 stake
            def pnl(side: str, win: bool) -> float:
                if not win:
                    return -1.0
                # Effective entry по side
                entry = mp if side == "YES" else (1.0 - mp)
                entry = max(entry, 0.01)
                return (1.0 / entry) - 1.0

            horizon_days = (end_date - dt.datetime.fromtimestamp(t, tz=dt.UTC)).total_seconds() / 86400

            def rnd(v: float | None, d: int = 6) -> float | None:
                return round(v, d) if v is not None else None

            out_rows.append({
                "condition_id": cid,
                "category": category or "Other",
                "phase": phase,
                "t_unix": t,
                "horizon_days": round(horizon_days, 2),
                "total_volume_usd": float(total_vol or 0),
                "market_price": round(mp, 6),
                "whale_raw": round(whale_r, 6),
                "maker_raw": round(maker_r, 6),
                "taker_raw": round(taker_r, 6),
                # Prod oracle
                "oracle_prod": round(oracle_prod["oracle_score"], 6),
                "div_prod": round(div_prod, 6),
                "z_post_prod": round(oracle_prod["z_posterior"], 6),
                "side_prod": side_prod,
                "prod_win": prod_win,
                "prod_pnl": round(pnl(side_prod, prod_win), 6),
                # Research oracle
                "oracle_research": round(oracle_research["oracle_score"], 6),
                "div_research": round(div_research, 6),
                "z_post_research": round(oracle_research["z_posterior"], 6),
                "side_research": side_research,
                "research_win": research_win,
                "research_pnl": round(pnl(side_research, research_win), 6),
                # Outcome
                "winner_is_yes": is_yes_winner,
                # === exploratory features ===
                "momentum_24h": rnd(snap.get("momentum_24h")),
                "price_24h_ago": rnd(snap.get("price_24h_ago")),
                "vwap_7d": rnd(snap.get("vwap_7d")),
                "std_first_24h": rnd(snap.get("std_first_24h")),
                "whale_buy_ratio_2h": rnd(snap.get("whale_buy_ratio_2h"), 4),
                "unique_takers_2h": snap.get("unique_takers_2h", 0),
                "volume_2h": rnd(snap.get("volume_2h"), 2),
                "median_hourly_volume_7d": rnd(snap.get("median_hourly_volume_7d"), 2),
            })
            processed += 1

        # Освобождаем trades для текущего market'а (важно в per_market mode)
        if bulk_mode == "per_market":
            del trades

        # Периодический flush + прогресс
        if (idx + 1) % 100 == 0 or idx == len(markets) - 1:
            elapsed = time.time() - t0
            rate = (idx + 1) / elapsed
            eta = (len(markets) - idx - 1) / rate if rate > 0 else 0
            print(
                f"      [{idx+1:>6}/{len(markets)}] "
                f"rows={len(out_rows):,} "
                f"errors={errors} stale={skipped_stale} "
                f"rate={rate:.1f} mkt/s "
                f"eta={eta/60:.1f}min",
                flush=True,
            )

    # ── 6. Запись результатов ──
    print(f"\n[6/6] Writing {len(out_rows):,} rows to {args.out}")
    if not out_rows:
        print("      NO ROWS — ничего не записано")
        return

    # Через pandas для простой parquet-записи
    import pandas as pd
    df = pd.DataFrame(out_rows)
    df.to_parquet(args.out, compression="snappy", index=False)

    # Метаданные калибровки — sidecar JSON
    meta_path = args.out + ".meta.json"
    with open(meta_path, "w") as f:
        json.dump({
            "generated_at": dt.datetime.now(dt.UTC).isoformat(),
            "markets_processed": len(markets),
            "rows_written": len(out_rows),
            "errors": errors,
            "skipped_stale": skipped_stale,
            "min_volume_usd": args.min_volume,
            "prod_calibration_snapshot": args.calibration,
            "research_weights_source": "BayesianScoreCalculator.DEFAULT_WEIGHTS",
            "research_weights": research_weights,
            "pit_phases": ["early", "mid", "late"],
            "window_sec": WINDOW_SEC,
            "max_price_staleness_sec": MAX_PRICE_STALENESS_SEC,
        }, f, indent=2)
    print(f"      metadata → {meta_path}")

    # Быстрая сводка по результатам
    print("\n=== SUMMARY ===")
    print(f"  Rows: {len(df):,}")
    print(f"  Markets: {df['condition_id'].nunique():,}")
    print(f"  Categories: {df['category'].value_counts().to_dict()}")
    print(f"  Phases: {df['phase'].value_counts().to_dict()}")
    print()
    print(f"  PROD WR: {df['prod_win'].mean():.1%}  ROI: {df['prod_pnl'].mean():.4f}")
    print(f"  RESEARCH WR: {df['research_win'].mean():.1%}  ROI: {df['research_pnl'].mean():.4f}")

    print(f"\nDONE in {(time.time()-t0)/60:.1f}min")


if __name__ == "__main__":
    main()
