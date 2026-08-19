"""Backfill detector_signals.event_size + archetype через HF parquet.

Stage 0 / Two-Stage ML Pipeline (HF coverage edition).

Проблема: detector_signals.event_id заполнен из HF (100% coverage) но events table
содержит только текущие polymarket events (10.6% overlap). JOIN-based backfill
покрывает только 18% строк.

Решение: посчитать event_size + archetype напрямую из HF parquet markets.parquet
для всех HF event_id и обновить detector_signals batch'ем.

Usage (на проде):
    docker cp scripts/backfill_detector_signals_event_meta_from_hf.py oraclepoly-backend:/app/scripts/
    docker exec -i oraclepoly-backend python scripts/backfill_detector_signals_event_meta_from_hf.py
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
import structlog

sys.path.insert(0, "/app")
sys.path.insert(0, ".")

from app.services.database import close_db, execute, fetchval, init_db  # noqa: E402

logger = structlog.get_logger()


# --- Inline classify_event (без зависимости от research/, которой нет в backend container) ---

LADDER_PATTERNS = [
    re.compile(r"\b(\d+)\s*[-–to]\s*(\d+)\b"),
    re.compile(r"\b(\d+)\s*\+\b"),
    re.compile(r"\bover\s+(\d+(?:\.\d+)?)\b", re.I),
    re.compile(r"\bunder\s+(\d+(?:\.\d+)?)\b", re.I),
    re.compile(r"\$(\d+(?:[\.,]\d+)?[KMB]?)\s*[-–to]\s*\$?(\d+(?:[\.,]\d+)?[KMB]?)\b", re.I),
]
VS_PATTERNS = [
    re.compile(r"\bvs\.?\s+", re.I),
    re.compile(r"\bvs?\s+\w+\s+win\b", re.I),
]
TIME_THRESHOLD_PATTERNS = [
    re.compile(r"\bby\s+(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+\d+", re.I),
    re.compile(r"\bbefore\s+\d+(?:\.|st|nd|rd|th)?", re.I),
    re.compile(r"\b(?:on|by)\s+\d{4}-\d{2}-\d{2}\b"),
]


@dataclass
class EventClassification:
    event_id: str
    n_markets: int
    archetype: str
    confidence: float = 0.5
    sum_yes_prices: float = 0.0
    examples: list[str] = field(default_factory=list)
    reasoning: str = ""


def classify_event(markets: list[dict[str, Any]]) -> EventClassification:
    """Inline copy from research/event_structure_audit.py:classify_event."""
    n = len(markets)
    if n == 1:
        return EventClassification(
            event_id=markets[0]["event_id"], n_markets=1, archetype="single_market", confidence=1.0,
        )

    questions = [m["question"] for m in markets]
    yes_prices = [float(m.get("current_price") or 0) for m in markets]
    sum_yes = sum(yes_prices)

    is_sum_one = 0.85 <= sum_yes <= 1.15
    has_ladder = sum(1 for q in questions if any(p.search(q) for p in LADDER_PATTERNS)) >= max(2, n // 2)
    has_vs = any(any(p.search(q) for p in VS_PATTERNS) for q in questions)
    has_time = sum(1 for q in questions if any(p.search(q) for p in TIME_THRESHOLD_PATTERNS)) >= max(2, n // 2)

    common_prefix_len = 0
    if len(questions) >= 2:
        ref = questions[0]
        for i in range(min(len(q) for q in questions)):
            if all(q[i] == ref[i] for q in questions):
                common_prefix_len = i + 1
            else:
                break
    high_similarity = common_prefix_len >= 20

    if has_ladder:
        return EventClassification(event_id=markets[0]["event_id"], n_markets=n, archetype="mutually_exclusive_ladder")
    if has_vs and is_sum_one:
        return EventClassification(event_id=markets[0]["event_id"], n_markets=n, archetype="one_of_n_choice")
    if has_time and high_similarity:
        return EventClassification(event_id=markets[0]["event_id"], n_markets=n, archetype="hierarchical_time")
    if is_sum_one:
        return EventClassification(event_id=markets[0]["event_id"], n_markets=n, archetype="one_of_n_choice")
    if high_similarity:
        return EventClassification(event_id=markets[0]["event_id"], n_markets=n, archetype="independent_multi")
    return EventClassification(event_id=markets[0]["event_id"], n_markets=n, archetype="independent_multi")

HF_MARKETS_PATH = "/mnt/data/polymarket_dataset/markets.parquet"


_COUNT_NULL_SQL = """
SELECT COUNT(*) FROM detector_signals
 WHERE resolved = true
   AND outcome IN ('WIN', 'LOSS')
   AND event_id IS NOT NULL
   AND (event_size IS NULL OR archetype IS NULL)
"""

_DISTINCT_EVENT_IDS_SQL = """
SELECT DISTINCT event_id
  FROM detector_signals
 WHERE resolved = true
   AND outcome IN ('WIN', 'LOSS')
   AND event_id IS NOT NULL
"""

# UNNEST batch UPDATE — атомарно обновляет все строки одного запроса.
_BATCH_UPDATE_SQL = """
UPDATE detector_signals
   SET event_size = b.event_size,
       archetype  = b.archetype
  FROM unnest($1::text[], $2::int[], $3::text[]) AS b(event_id, event_size, archetype)
 WHERE detector_signals.event_id = b.event_id
"""


def _build_event_meta_from_hf() -> dict[str, dict[str, Any]]:
    """Читает HF parquet, группирует markets по event_id, классифицирует.

    Returns: {event_id: {event_size: int, archetype: str}}
    """
    logger.info("hf_markets_load_start", path=HF_MARKETS_PATH)
    df = pd.read_parquet(
        HF_MARKETS_PATH,
        columns=["event_id", "id", "question", "outcome_prices"],
    )
    df = df[df["event_id"].notna()].copy()
    df["event_id"] = df["event_id"].astype(str)
    logger.info("hf_markets_load_done", rows=len(df), distinct_events=df["event_id"].nunique())

    # Group markets per event
    by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in df.itertuples(index=False):
        # outcome_prices = "[0.55, 0.45]" string или list
        prices = row.outcome_prices
        try:
            if isinstance(prices, str):
                import ast

                prices = ast.literal_eval(prices)
            yes_price = float(prices[0]) if prices and len(prices) > 0 else 0.0
        except (ValueError, SyntaxError, IndexError, TypeError):
            yes_price = 0.0

        by_event[str(row.event_id)].append({
            "event_id": str(row.event_id),
            "market_id": str(row.id),
            "question": str(row.question or ""),
            "current_price": yes_price,
            "resolved": True,
        })

    # Classify each event
    out: dict[str, dict[str, Any]] = {}
    for ev_id, mkts in by_event.items():
        try:
            cls = classify_event(mkts)
            out[ev_id] = {"event_size": cls.n_markets, "archetype": cls.archetype}
        except Exception as e:
            # fallback: independent_multi
            out[ev_id] = {"event_size": len(mkts), "archetype": "independent_multi"}
            logger.warning("classify_event_failed", event_id=ev_id, n_markets=len(mkts), error=str(e))
    logger.info("hf_classify_done", events=len(out))
    return out


async def run(dry_run: bool = False, batch_size: int = 5000) -> dict[str, Any]:
    await init_db(min_size=1, max_size=4)
    try:
        before = await fetchval(_COUNT_NULL_SQL)
        logger.info("backfill_from_hf_start", null_rows=before)

        meta = _build_event_meta_from_hf()
        if dry_run:
            logger.info("dry_run_skip_db_writes", events_classified=len(meta))
            return {"dry_run": True, "events_classified": len(meta)}

        # Достаём все distinct event_id из detector_signals
        from app.services.database import fetch

        rows = await fetch(_DISTINCT_EVENT_IDS_SQL)
        ds_evids = [str(r["event_id"]) for r in rows]
        logger.info("ds_distinct_evids", n=len(ds_evids))

        # Что покрывается HF
        evids_to_update = [e for e in ds_evids if e in meta]
        not_in_hf = len(ds_evids) - len(evids_to_update)
        logger.info(
            "evids_coverage",
            ds_distinct=len(ds_evids),
            covered_by_hf=len(evids_to_update),
            not_in_hf=not_in_hf,
        )

        # Batch update
        total_updated = 0
        for i in range(0, len(evids_to_update), batch_size):
            chunk = evids_to_update[i : i + batch_size]
            ev_ids = chunk
            sizes = [int(meta[e]["event_size"]) for e in chunk]
            archs = [str(meta[e]["archetype"]) for e in chunk]
            status = await execute(_BATCH_UPDATE_SQL, ev_ids, sizes, archs)
            try:
                updated = int(status.split()[1]) if status and status.startswith("UPDATE ") else 0
            except (IndexError, ValueError):
                updated = 0
            total_updated += updated
            logger.info("batch_done", batch_start=i, batch_size=len(chunk), updated=updated)

        after = await fetchval(_COUNT_NULL_SQL)
        result = {
            "rows_with_null_before": int(before or 0),
            "rows_with_null_after": int(after or 0),
            "rows_updated": total_updated,
            "events_in_hf": len(meta),
            "events_in_ds": len(ds_evids),
            "events_covered": len(evids_to_update),
        }
        logger.info("backfill_from_hf_done", **result)
        return result
    finally:
        await close_db()


def _main() -> None:
    parser = argparse.ArgumentParser(description="Backfill event_size+archetype from HF parquet")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(run(dry_run=args.dry_run))
    print(result)


if __name__ == "__main__":
    _main()
