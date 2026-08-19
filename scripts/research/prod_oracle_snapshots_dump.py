"""Дамп resolved oracle_snapshots из прод PostgreSQL в локальный parquet.

Используется для backtest'а реального прод-oracle (вместо реконструкции из HF):
все факторы уже посчитаны продом в момент T, нам нужно только сравнить с baseline'ами.

Контекст: альтернатива "Λ_registry PIT restore" варианту — зачем восстанавливать
факторы из истории, если прод уже пишет их в oracle_snapshots?

Что делает:
1. Через SSH → docker exec psql → COPY TO STDOUT дампает ~367K resolved snapshot'ов
2. Загружает локально через pandas/duckdb
3. Конвертирует в parquet (snappy)
4. Печатает summary: row count, date range, category distribution, factor coverage

Результат (~50-60 MB):
    data/prod_oracle_snapshots.parquet

Использование:
    python3 scripts/research/prod_oracle_snapshots_dump.py

Предусловия:
- SSH key к root@<YOUR_SERVER_IP> настроен
- docker контейнер oraclepoly-postgres запущен на проде

Ссылки:
- Schema: docs/research/2026-04-10-oracle-pit-findings.md (oracle_snapshots section)
- Upstream consumer: scripts/research/prod_oracle_snapshots_backtest.py
"""

import subprocess
import sys
from pathlib import Path

SSH_HOST = "root@<YOUR_SERVER_IP>"
SSH_PORT = "2222"
DOCKER_CMD = "docker exec -i oraclepoly-postgres psql -U oraclepoly -d oraclepoly -c"

OUT_DIR = Path(__file__).parent.parent.parent / "data"
OUT_PATH = OUT_DIR / "prod_oracle_snapshots.parquet"

# Минимальный набор колонок — всё что нужно для backtest baseline-matrix.
# Убрал JOIN с markets (question не нужен для backtest).
DUMP_SQL = """
COPY (
    SELECT
        id::text, market_id::text, oracle_score, market_price,
        outcome, prediction_correct, brier_score,
        created_at, resolved_at, end_date, category, divergence, divergence_signal,
        whale_contribution_raw, news_contribution_raw, z_posterior,
        oi_factor_raw, concentration_factor_raw, micro_factor_raw,
        sm_fresh_raw, sm_cluster_raw, sm_registry_raw,
        sports_odds_raw, retail_factor_raw, maker_direction_raw, taker_imbalance_raw,
        volume_24h, news_count, whale_tx_count, final_price
    FROM oracle_snapshots
    WHERE outcome IN ('yes', 'no')
      AND resolved_at IS NOT NULL
    ORDER BY created_at ASC
) TO STDOUT WITH CSV HEADER
"""


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)

    print("=" * 70)
    print("PROD ORACLE_SNAPSHOTS DUMP")
    print("=" * 70)
    print(f"Target: {OUT_PATH}")

    # Шаг 1 — достаём CSV через SSH + docker exec psql.
    # COPY TO STDOUT отдаёт поток без промежуточных файлов на VPS.
    print("\n[1/3] Dumping via SSH → docker exec psql")

    # COPY не любит многострочный SQL с -c. Надо однострочный.
    one_line_sql = " ".join(DUMP_SQL.split())
    remote_cmd = f'{DOCKER_CMD} "{one_line_sql}"'
    ssh_cmd = ["ssh", "-p", SSH_PORT, SSH_HOST, remote_cmd]

    csv_path = OUT_DIR / "prod_oracle_snapshots.csv.tmp"
    if csv_path.exists() and csv_path.stat().st_size > 1024 * 1024:
        csv_size = csv_path.stat().st_size
        print(f"      CSV already exists ({csv_size / 1024 / 1024:.1f} MB), reusing")
    else:
        with open(csv_path, "wb") as f:
            result = subprocess.run(ssh_cmd, stdout=f, stderr=subprocess.PIPE, check=False)
        if result.returncode != 0:
            print(f"      SSH failed: {result.stderr.decode()[:500]}")
            sys.exit(1)
        csv_size = csv_path.stat().st_size
        print(f"      CSV dumped: {csv_size / 1024 / 1024:.1f} MB at {csv_path}")

    # Шаг 2 — конвертация CSV → parquet через duckdb (без промежуточного pandas).
    print("\n[2/3] Converting CSV → parquet via DuckDB")
    import duckdb

    con = duckdb.connect()
    # Явная схема — read_csv_auto агрессивно конвертирует yes/no → BOOLEAN
    # и колонки с многими NULL (oi_factor_raw и т.д.) → VARCHAR. Оба некорректно.
    con.execute(f"""
        CREATE TABLE snap AS
        SELECT
            id, market_id,
            CAST(oracle_score AS DOUBLE) AS oracle_score,
            CAST(market_price AS DOUBLE) AS market_price,
            CAST(outcome AS VARCHAR) AS outcome,
            CAST(prediction_correct AS BOOLEAN) AS prediction_correct,
            CAST(brier_score AS DOUBLE) AS brier_score,
            CAST(created_at AS TIMESTAMP) AS created_at,
            CAST(resolved_at AS TIMESTAMP) AS resolved_at,
            CAST(end_date AS TIMESTAMP) AS end_date,
            category,
            CAST(divergence AS DOUBLE) AS divergence,
            divergence_signal,
            CAST(whale_contribution_raw AS DOUBLE) AS whale_contribution_raw,
            CAST(news_contribution_raw AS DOUBLE) AS news_contribution_raw,
            CAST(z_posterior AS DOUBLE) AS z_posterior,
            CAST(oi_factor_raw AS DOUBLE) AS oi_factor_raw,
            CAST(concentration_factor_raw AS DOUBLE) AS concentration_factor_raw,
            CAST(micro_factor_raw AS DOUBLE) AS micro_factor_raw,
            CAST(sm_fresh_raw AS DOUBLE) AS sm_fresh_raw,
            CAST(sm_cluster_raw AS DOUBLE) AS sm_cluster_raw,
            CAST(sm_registry_raw AS DOUBLE) AS sm_registry_raw,
            CAST(sports_odds_raw AS DOUBLE) AS sports_odds_raw,
            CAST(retail_factor_raw AS DOUBLE) AS retail_factor_raw,
            CAST(maker_direction_raw AS DOUBLE) AS maker_direction_raw,
            CAST(taker_imbalance_raw AS DOUBLE) AS taker_imbalance_raw,
            CAST(volume_24h AS DOUBLE) AS volume_24h,
            CAST(news_count AS BIGINT) AS news_count,
            CAST(whale_tx_count AS BIGINT) AS whale_tx_count,
            CAST(final_price AS DOUBLE) AS final_price
        FROM read_csv_auto('{csv_path}', header=true, all_varchar=true)
    """)
    n = con.execute("SELECT COUNT(*) FROM snap").fetchone()[0]
    print(f"      loaded: {n:,} rows")

    con.execute(f"COPY snap TO '{OUT_PATH}' (FORMAT PARQUET, COMPRESSION SNAPPY)")
    print(f"      parquet written: {OUT_PATH.stat().st_size / 1024 / 1024:.1f} MB")

    # Шаг 3 — summary.
    print("\n[3/3] Summary")
    stats = con.execute("""
        SELECT
            COUNT(*) as n,
            MIN(created_at) as min_ts,
            MAX(created_at) as max_ts,
            SUM(CASE WHEN outcome = 'yes' THEN 1 ELSE 0 END) as yes_n,
            SUM(CASE WHEN outcome = 'no' THEN 1 ELSE 0 END) as no_n,
            SUM(CASE WHEN whale_contribution_raw IS NOT NULL THEN 1 ELSE 0 END) as whale_n,
            SUM(CASE WHEN news_contribution_raw IS NOT NULL THEN 1 ELSE 0 END) as news_n,
            SUM(CASE WHEN sm_registry_raw IS NOT NULL AND sm_registry_raw != 0 THEN 1 ELSE 0 END) as sm_n,
            SUM(CASE WHEN retail_factor_raw IS NOT NULL AND retail_factor_raw != 0 THEN 1 ELSE 0 END) as retail_n,
            SUM(CASE WHEN maker_direction_raw IS NOT NULL AND maker_direction_raw != 0 THEN 1 ELSE 0 END) as maker_n,
            SUM(CASE WHEN taker_imbalance_raw IS NOT NULL AND taker_imbalance_raw != 0 THEN 1 ELSE 0 END) as taker_n,
            SUM(CASE WHEN sports_odds_raw IS NOT NULL AND sports_odds_raw != 0 THEN 1 ELSE 0 END) as sports_n
        FROM snap
    """).fetchone()

    (
        n, min_ts, max_ts, yes_n, no_n,
        whale_n, news_n, sm_n, retail_n, maker_n, taker_n, sports_n,
    ) = stats

    print(f"      rows: {n:,}")
    print(f"      date range: {min_ts} → {max_ts}")
    print(f"      outcomes: yes={yes_n:,} ({yes_n/n:.1%})  no={no_n:,} ({no_n/n:.1%})")
    print("\n      Factor coverage (non-null, non-zero):")
    print(f"        whale_contribution_raw:  {whale_n:>7,} ({whale_n/n:.1%})")
    print(f"        news_contribution_raw:   {news_n:>7,} ({news_n/n:.1%})")
    print(f"        sm_registry_raw:         {sm_n:>7,} ({sm_n/n:.1%})")
    print(f"        sports_odds_raw:         {sports_n:>7,} ({sports_n/n:.1%})")
    print(f"        retail_factor_raw:       {retail_n:>7,} ({retail_n/n:.1%})")
    print(f"        maker_direction_raw:     {maker_n:>7,} ({maker_n/n:.1%})")
    print(f"        taker_imbalance_raw:     {taker_n:>7,} ({taker_n/n:.1%})")

    cats = con.execute("""
        SELECT category, COUNT(*) as n
        FROM snap
        GROUP BY category
        ORDER BY n DESC
    """).fetchall()
    print("\n      Category distribution:")
    for cat, cnt in cats:
        cat_str = cat if cat else "(NULL)"
        print(f"        {cat_str:<20} {cnt:>7,} ({cnt/n:.1%})")

    # Cleanup
    csv_path.unlink()
    print("\nDONE")


if __name__ == "__main__":
    main()
