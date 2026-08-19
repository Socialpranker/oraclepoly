"""Stage 5: TG alerts — format and send messages для novel candidates.

Silent if 0 novel (per user preference). Cross-week suppression via
notified_at column. Hard cap 5 messages per scan.
"""

from __future__ import annotations

import html
import logging
from pathlib import Path

import asyncpg

from .config import (
    MAX_NOVEL_ALERTS_PER_RUN,
    MAX_TOTAL_MESSAGES_PER_RUN,
    SUPPRESS_REPEAT_DAYS,
)
from .persist import ClassifiedResult

logger = logging.getLogger(__name__)


_RECENT_NOTIFIED_SQL = f"""
SELECT 1 FROM auto_edge_discoveries
 WHERE cell_signature = $1
   AND notified_at IS NOT NULL
   AND notified_at >= now() - INTERVAL '{SUPPRESS_REPEAT_DAYS} days'
 LIMIT 1
"""


_MARK_NOTIFIED_SQL = """
UPDATE auto_edge_discoveries
   SET notified_at = now()
 WHERE cell_signature = $1 AND scan_run_id = $2
"""


async def already_notified_recently(
    conn: asyncpg.Connection,
    cell_signature: str,
) -> bool:
    """Check if signature was notified within last SUPPRESS_REPEAT_DAYS."""
    row = await conn.fetchrow(_RECENT_NOTIFIED_SQL, cell_signature)
    return row is not None


def build_alert_message(
    cr: ClassifiedResult,
    md_report_path: Path | str,
    rank: int,
) -> str:
    """Format HTML alert message for one candidate."""
    r = cr.result
    c = r.cell
    cat_str = c.category or "ALL"
    op = c.threshold_op
    val = f"{c.threshold_value:.4g}"
    range_str = f"[{c.zone_lo:.2f}, {c.zone_hi:.2f})"

    sustained = ""
    if cr.n_times_seen >= 2:
        sustained = f" (seen {cr.n_times_seen}× — sustained!)"

    year_split_line = ""
    if r.year_2025_net_roi is not None and r.year_2026_net_roi is not None:
        year_split_line = (
            f"\n<b>Year split:</b> 2025 {r.year_2025_net_roi*100:+.1f}%, "
            f"2026 {r.year_2026_net_roi*100:+.1f}%"
        )

    fdr_line = ""
    if r.bh_adjusted_p is not None:
        fdr_line = f"\n<b>FDR q-value:</b> {r.bh_adjusted_p:.3f}"

    md_str = str(md_report_path)
    text = (
        f"🎯 <b>New Edge Candidate #{rank}</b>{sustained}\n"
        f"<b>Feature:</b> <code>{html.escape(c.feature)} {html.escape(op)} {val}</code>\n"
        f"<b>Zone:</b> {html.escape(c.zone)} {range_str}, "
        f"Anchor {c.anchor}h, Category {html.escape(cat_str)}\n"
        f"<b>Side:</b> {c.side}\n"
        f"<b>Stats:</b> n={r.n_signals:,} ({r.n_unique_markets:,} markets), "
        f"WR {r.win_rate*100:.1f}%, net_ROI {r.net_roi*100:+.1f}%\n"
        f"<b>Mirror:</b> {r.mirror_net_roi*100:+.1f}% "
        f"(diff {r.mirror_diff_pp:+.1f}pp)"
        f"{year_split_line}\n"
        f"<b>Capacity:</b> {r.daily_raw_pnl:+.2f}$/d raw, "
        f"{r.daily_honest_pnl:+.2f}$/d honest @5K\n"
        f"<b>Verdict:</b> {r.verdict} — overlap_max {cr.overlap_max*100:.0f}%, "
        f"<b>NOVEL</b>"
        f"{fdr_line}\n"
        f"<b>Report:</b> <code>{html.escape(md_str)}</code>"
    )
    return text


async def send_alerts(
    conn: asyncpg.Connection,
    scan_run_id,
    classified: list[ClassifiedResult],
    md_report_path: Path | str,
    *,
    send_tg: bool = True,
    send_admin_message_fn=None,
) -> int:
    """Send TG alerts for top novel candidates. Returns count sent.

    Silent if 0 eligible novel (no message). Suppresses signatures notified
    within SUPPRESS_REPEAT_DAYS.

    `send_admin_message_fn` is optional override for testing. If None,
    imports app.tasks.notifications.send_admin_message at runtime.
    """
    novel_eligible = [
        c for c in classified
        if c.status == "novel" and c.result.verdict in ("PASS", "MARGINAL")
    ]
    if not novel_eligible:
        logger.info("notify_no_novel_silent")
        return 0

    # Filter cross-week suppressed
    fresh = []
    for cr in novel_eligible:
        if await already_notified_recently(conn, cr.cell_signature):
            logger.info(
                "notify_suppressed_repeat",
                extra={"signature": cr.cell_signature},
            )
            continue
        fresh.append(cr)

    if not fresh:
        logger.info("notify_all_suppressed")
        return 0

    # Top N by rank_score
    fresh.sort(key=lambda c: c.rank_score, reverse=True)
    top = fresh[:MAX_NOVEL_ALERTS_PER_RUN]

    # Hard cap MAX_TOTAL
    top = top[:MAX_TOTAL_MESSAGES_PER_RUN]

    if not send_tg:
        logger.info(
            "notify_dry_run",
            extra={"would_send_count": len(top)},
        )
        return 0

    # Lazy import
    if send_admin_message_fn is None:
        from app.tasks.notifications import send_admin_message
        send_admin_message_fn = send_admin_message

    n_sent = 0
    for rank, cr in enumerate(top, start=1):
        try:
            text = build_alert_message(cr, md_report_path, rank)
            await send_admin_message_fn(text)
            await conn.execute(_MARK_NOTIFIED_SQL, cr.cell_signature, scan_run_id)
            n_sent += 1
        except Exception as exc:
            logger.warning(
                "notify_send_failed",
                extra={"signature": cr.cell_signature, "error": str(exc)},
            )
    logger.info("notify_alerts_sent", extra={"count": n_sent})
    return n_sent
