"""Регрессионный тест для chunked bulk fetch в historical_pit_loader.

История: предыдущая версия `fetch_all_trades_bulk` использовала один SELECT WHERE
condition_id IN (10K) + .fetchall() в Python list — на VPS 8GB вызывала OOM kill
(7.5GB RSS). Новая версия чанкит запрос по 200 markets через fetch_arrow_table().

Тест:
1. Создаёт временный parquet с 1000 фейковыми markets × 100 trades each (100K rows).
2. Запускает fetch_all_trades_bulk с разными chunk_size (50, 200, 1000).
3. Проверяет что результат identичный (тот же dict cid → trades).
4. Проверяет что function не падает на пустом списке cids.
5. Проверяет что chunk_size меньше N — обрабатывает все markets корректно.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import duckdb
import pytest

# sys.path setup: 3 levels up from tests/
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(scope="module")
def fake_quant_parquet():
    """Создать temp parquet с 1000 markets × 100 trades each = 100K rows."""
    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as f:
        tmp_path = f.name

    con = duckdb.connect()
    con.execute(f"""
        COPY (
            SELECT
                'cid_' || lpad(CAST(m AS VARCHAR), 4, '0') AS condition_id,
                1700000000 + m * 1000 + t * 10 AS timestamp,
                0.4 + (m * 0.0001) AS price,
                100.0 + t AS usd_amount,
                CASE WHEN t % 2 = 0 THEN 'YES' ELSE 'NO' END AS side,
                'taker_' || CAST((m + t) % 100 AS VARCHAR) AS taker
            FROM range(1000) AS r1(m), range(100) AS r2(t)
        ) TO '{tmp_path}' (FORMAT 'PARQUET')
    """)
    con.close()

    yield tmp_path
    os.unlink(tmp_path)


def test_fetch_all_trades_bulk_chunked_returns_correct_dict(fake_quant_parquet: str):
    """Базовый smoke: fetch на 1000 markets с chunk_size=200 даёт ожидаемое."""
    from scripts.research.historical_pit_loader import fetch_all_trades_bulk

    cids = [f"cid_{i:04d}" for i in range(1000)]
    con = duckdb.connect()
    con.execute("SET memory_limit='1GB';")

    result = fetch_all_trades_bulk(con, cids, fake_quant_parquet, chunk_size=200)

    assert len(result) == 1000, f"Expected 1000 markets, got {len(result)}"
    assert all(len(trades) == 100 for trades in result.values()), "Each market should have 100 trades"
    assert "cid_0000" in result and "cid_0999" in result
    # Trades sorted by timestamp ASC внутри каждого market
    for cid, trades in result.items():
        timestamps = [t[0] for t in trades]
        assert timestamps == sorted(timestamps), f"{cid} trades not sorted"


def test_fetch_chunk_size_does_not_affect_result(fake_quant_parquet: str):
    """Регрессия: chunk_size=50 vs 200 vs 1000 → identичный output."""
    from scripts.research.historical_pit_loader import fetch_all_trades_bulk

    cids = [f"cid_{i:04d}" for i in range(500)]
    con = duckdb.connect()

    r_small = fetch_all_trades_bulk(con, cids, fake_quant_parquet, chunk_size=50)
    r_medium = fetch_all_trades_bulk(con, cids, fake_quant_parquet, chunk_size=200)
    r_large = fetch_all_trades_bulk(con, cids, fake_quant_parquet, chunk_size=1000)

    assert set(r_small.keys()) == set(r_medium.keys()) == set(r_large.keys())
    for cid in cids:
        assert r_small[cid] == r_medium[cid] == r_large[cid], (
            f"chunk_size affects result for {cid}"
        )


def test_fetch_handles_empty_input(fake_quant_parquet: str):
    """Edge case: пустой список cids не должен падать."""
    from scripts.research.historical_pit_loader import fetch_all_trades_bulk

    con = duckdb.connect()
    result = fetch_all_trades_bulk(con, [], fake_quant_parquet, chunk_size=200)
    assert result == {}


def test_fetch_handles_missing_cids(fake_quant_parquet: str):
    """Edge case: cid'ы которых нет в parquet → отсутствуют в результате (не падают)."""
    from scripts.research.historical_pit_loader import fetch_all_trades_bulk

    con = duckdb.connect()
    # cid_9999 не существует (parquet содержит только 0000-0999)
    cids = ["cid_0000", "cid_9999", "cid_0001"]
    result = fetch_all_trades_bulk(con, cids, fake_quant_parquet, chunk_size=200)

    assert "cid_0000" in result
    assert "cid_0001" in result
    assert "cid_9999" not in result, "Non-existent cid should not appear in result"
    assert len(result["cid_0000"]) == 100


def test_fetch_uses_fetch_arrow_table_not_fetchall():
    """Регрессия: убедиться что новая реализация использует fetch_arrow_table()
    (memory-efficient), а не .fetchall() (Python list — OOM-prone).

    AST-based check: ищем `fetch_arrow_table` в исходнике fetch_all_trades_bulk.
    """
    import ast

    src_path = Path(__file__).parent.parent / "scripts" / "research" / "historical_pit_loader.py"
    src = src_path.read_text()
    tree = ast.parse(src)

    target_func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "fetch_all_trades_bulk":
            target_func = node
            break

    assert target_func is not None, "fetch_all_trades_bulk не найдена"

    func_src = ast.unparse(target_func)
    # Должен использовать Arrow API (to_arrow_table или fetch_arrow_table) — не Python list
    has_arrow = "to_arrow_table" in func_src or "fetch_arrow_table" in func_src
    assert has_arrow, (
        "fetch_all_trades_bulk должна использовать Arrow API (to_arrow_table/fetch_arrow_table) "
        "для memory efficiency. .fetchall() вызывает OOM на VPS 8GB."
    )
    assert "chunk_size" in func_src, (
        "fetch_all_trades_bulk должна принимать chunk_size параметр для chunking."
    )
