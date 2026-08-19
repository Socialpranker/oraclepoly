#!/usr/bin/env python3
"""Auto Edge Scanner — weekly automated edge discovery.

Orchestrator for 6-stage pipeline:
  Stage 1: grid_generator — enumerate cells
  Stage 2: gate — strict gate + FDR
  Stage 3: dedup — territory overlap
  Stage 4: persist — DB + MD + CSV
  Stage 5: notify — TG alerts (silent if 0 novel)
  Stage 6: cron — installed via crontab.conf

Usage:
    /root/oracle/.venv-research/bin/python scripts/auto_edge_scanner.py [--dry-run] [--no-tg]

Plan: ~/.claude/plans/wiggly-nibbling-stardust.md
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb

# Make package importable when run as script
sys.path.insert(0, str(Path(__file__).parent.parent))

# Load .env from project root so DATABASE_URL is available without cron 'source .env'.
try:
    from dotenv import load_dotenv  # noqa: E402

    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    # python-dotenv не критичен — env может быть передан через cron `source .env`.
    pass

from scripts.research.edge_scanner.config import ML_FEATURES_PATH  # noqa: E402
from scripts.research.edge_scanner.dedup import classify_cell  # noqa: E402
from scripts.research.edge_scanner.gate import (  # noqa: E402
    apply_fdr,
    evaluate_cell,
    rank_score,
)
from scripts.research.edge_scanner.grid_generator import generate_cells  # noqa: E402
from scripts.research.edge_scanner.notify import send_alerts  # noqa: E402
from scripts.research.edge_scanner.persist import (  # noqa: E402
    ClassifiedResult,
    get_db_conn,
    get_signature_history,
    insert_results,
    write_csv,
    write_md_report,
)

logger = logging.getLogger(__name__)


def _setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )


async def run_scan(
    parquet_path: str = ML_FEATURES_PATH,
    *,
    dry_run: bool = False,
    no_tg: bool = False,
    scan_date_override: date | None = None,
) -> dict:
    """Run full scan pipeline. Returns summary dict.

    dry_run=True: skip DB writes + TG send (only render MD + CSV).
    no_tg=True: write to DB but skip TG alerts.
    """
    t0 = time.time()
    scan_run_id = uuid.uuid4()
    scan_date = scan_date_override or datetime.now(UTC).date()

    logger.info(
        "auto_edge_scan_starting",
        extra={
            "scan_run_id": str(scan_run_id),
            "scan_date": scan_date.isoformat(),
            "dry_run": dry_run,
            "no_tg": no_tg,
        },
    )

    # Stage 1: Generate cells
    cells, grid_stats = generate_cells(parquet_path)
    logger.info("auto_edge_stage1_done", extra=grid_stats)

    # Stage 2: Evaluate cells
    con = duckdb.connect()
    con.execute("SET memory_limit='4GB'")
    con.execute("SET threads=4")

    results = []
    n_evaluated = 0
    for cell in cells:
        n_evaluated += 1
        if n_evaluated % 1000 == 0:
            logger.info(
                "auto_edge_stage2_progress",
                extra={
                    "evaluated": n_evaluated,
                    "total": len(cells),
                    "passed_so_far": len(results),
                },
            )
        result = evaluate_cell(con, cell, parquet_path)
        if result is not None:
            results.append(result)

    apply_fdr(results)
    logger.info(
        "auto_edge_stage2_done",
        extra={"n_evaluated": n_evaluated, "n_passed_gate": len(results)},
    )

    # Stage 3: Classify (dedup)
    classified: list[ClassifiedResult] = []
    for r in results:
        status, overlaps = classify_cell(con, r, parquet_path)
        overlap_max = max(overlaps.values()) if overlaps else 0.0
        score = rank_score(r, overlap_max)
        cell_sig = r.cell.signature()
        classified.append(
            ClassifiedResult(
                result=r,
                status=status,
                overlaps=overlaps,
                overlap_max=overlap_max,
                rank_score=score,
                cell_signature=cell_sig,
                first_seen_scan_date=None,  # filled by Stage 4 from DB
                n_times_seen=1,
            )
        )

    n_novel = sum(1 for c in classified if c.status == "novel")
    n_subset = sum(1 for c in classified if c.status == "subset")
    n_partial = sum(1 for c in classified if c.status == "partial_overlap")
    logger.info(
        "auto_edge_stage3_done",
        extra={"novel": n_novel, "subset": n_subset, "partial_overlap": n_partial},
    )

    # Stage 4: Persist
    if not dry_run:
        conn = await get_db_conn()
        try:
            # Fill cross-week history per signature
            for cr in classified:
                first_seen, n_seen = await get_signature_history(conn, cr.cell_signature)
                cr.first_seen_scan_date = first_seen or scan_date
                cr.n_times_seen = n_seen

            n_inserted = await insert_results(conn, scan_run_id, scan_date, classified)
            logger.info(
                "auto_edge_stage4_db_done",
                extra={"inserted": n_inserted, "total": len(classified)},
            )
        except Exception as exc:
            logger.error("auto_edge_stage4_db_failed", extra={"error": str(exc)})
            raise
    else:
        # Dry run: skip DB but still try to fetch signature history if connection works
        logger.info("auto_edge_stage4_dry_run_skip_db")

    md_path = write_md_report(classified, scan_date, grid_stats["total_enumerated"], n_evaluated)
    csv_path = write_csv(classified, scan_date)
    logger.info(
        "auto_edge_stage4_files_written",
        extra={"md": str(md_path), "csv": str(csv_path)},
    )

    # Stage 5: Notify
    n_alerts = 0
    if not dry_run and not no_tg:
        try:
            n_alerts = await send_alerts(conn, scan_run_id, classified, md_path)
        except Exception as exc:
            logger.warning("auto_edge_stage5_notify_failed", extra={"error": str(exc)})
        finally:
            await conn.close()
    elif not dry_run:
        # Connection still open from Stage 4, close it
        await conn.close()

    elapsed = time.time() - t0
    summary = {
        "scan_run_id": str(scan_run_id),
        "scan_date": scan_date.isoformat(),
        "cells_enumerated": grid_stats["total_enumerated"],
        "cells_killed_filtered": grid_stats["n_killed_filtered"],
        "cells_evaluated": n_evaluated,
        "cells_passed_gate": len(results),
        "n_novel": n_novel,
        "n_subset": n_subset,
        "n_partial_overlap": n_partial,
        "tg_alerts_sent": n_alerts,
        "md_report": str(md_path),
        "csv_export": str(csv_path),
        "elapsed_sec": round(elapsed, 1),
        "dry_run": dry_run,
        "no_tg": no_tg,
    }
    logger.info("auto_edge_scan_completed", extra=summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description="Auto Edge Scanner — weekly hypothesis discovery")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip DB writes + TG send (only render MD + CSV)",
    )
    parser.add_argument(
        "--no-tg",
        action="store_true",
        help="Write to DB but skip TG alerts",
    )
    parser.add_argument(
        "--parquet",
        default=ML_FEATURES_PATH,
        help=f"Path to canonical parquet (default {ML_FEATURES_PATH})",
    )
    args = parser.parse_args()

    _setup_logging()

    summary = asyncio.run(
        run_scan(
            parquet_path=args.parquet,
            dry_run=args.dry_run,
            no_tg=args.no_tg,
        )
    )
    print("\n" + "=" * 60)
    print("AUTO EDGE SCANNER — SUMMARY")
    print("=" * 60)
    for k, v in summary.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    main()
