#!/usr/bin/env python3
"""Feature parity check — детектирует drift между canonical и live SQL kernels.

Запускается weekly (Sunday 05:00 UTC). Для N markets резолвнутых на этой неделе:
  1. Читает features из canonical parquet (ml_features.parquet) для (cid, anchor=24h)
  2. Запускает kernel SQL через DuckDB поверх live_* tables (если они есть в БД дамп)
     для тех же (cid, anchor=24h) — НО эти markets уже резолвнуты, значит они
     были в live до того как resolved. Smart trick: используем live tables
     pretending тот market был active.
  3. Diff column-by-column. Tolerance 1e-6.

ОГРАНИЧЕНИЕ: live_* таблицы не сохраняют историю после resolve (cleanup 31d).
Если parity check запустить на market резолвнутый >31 дней назад — live таблицы
пустые. Поэтому проверяем ТОЛЬКО recent resolved (last 7 days).

Output: stdout report + Telegram alert через TG_BOT_TOKEN если diff > 1e-6.

Запуск на VPS:
    cd ~/oracle && /root/oracle/.venv-research/bin/python scripts/research/feature_parity_check.py

См. docs/superpowers/plans/bright-hopping-meteor.md (Day 4).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# Allow standalone script
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import duckdb  # noqa: E402

from app.ml.feature_kernel import FEATURE_COLUMNS, build_features_sql  # noqa: E402

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

CANONICAL_PARQUET = "/mnt/data/canonical/ml_features.parquet"
ANCHOR_FOR_CHECK = 24  # самый используемый anchor
TOLERANCE = 1e-6
N_MARKETS_TO_CHECK = 5

# Колонки которые фундаментально могут отличаться (live имеет более свежие values
# или другой smart_money snapshot) — игнорируются в diff. Обоснование в plans/.
SKIP_COLUMNS = {
    "computed_at",  # отсутствует в parquet
    # Wallet flow на live использует current smart_money set, на canonical — historical.
    # Если smart_money refresh prior к resolved date — будет drift. Документируем.
    "n_unique_takers_24h",
    "whale_taker_volume_24h",
    "whale_taker_count_24h",
    "whale_buy_pressure_24h",
    "taker_hhi_proxy_24h",
    "repeat_taker_ratio_24h",
}


async def _send_tg_alert(message: str) -> None:
    """Best-effort Telegram alert."""
    try:
        from app.core.config import get_settings

        settings = get_settings()
        if not settings.telegram_bot_token or not settings.admin_telegram_ids:
            log.info("TG alert skipped (no token/admins): %s", message[:100])
            return
        import httpx

        admin_id = settings.admin_telegram_ids[0]
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                json={"chat_id": admin_id, "text": message[:4000]},
            )
    except Exception as exc:
        log.warning("TG alert failed: %s", exc)


async def _fetch_recent_resolved_markets() -> list[str]:
    """Get N condition_ids резолвнутых на этой неделе."""
    from app.services.database import fetch

    rows = await fetch(
        f"""
        SELECT condition_id
          FROM markets
         WHERE resolved = true
           AND end_date > now() - INTERVAL '7 days'
           AND end_date < now()
           AND condition_id IS NOT NULL
         ORDER BY end_date DESC
         LIMIT {N_MARKETS_TO_CHECK}
        """
    )
    return [r["condition_id"] for r in rows]


def _read_canonical_features(condition_ids: list[str], anchor_h: int) -> dict[str, dict]:
    """Read features из ml_features.parquet через DuckDB."""
    if not Path(CANONICAL_PARQUET).exists():
        return {}
    con = duckdb.connect()
    cids_csv = ", ".join(f"'{c}'" for c in condition_ids)
    rows = con.execute(f"""
        SELECT *
        FROM read_parquet('{CANONICAL_PARQUET}')
        WHERE anchor_hours = {anchor_h}
          AND condition_id IN ({cids_csv})
    """).fetchall()
    cols = [d[0] for d in con.description]
    return {r[cols.index("condition_id")]: dict(zip(cols, r, strict=False)) for r in rows}


async def _read_live_features(condition_ids: list[str], anchor_h: int) -> dict[str, dict]:
    """Read live features напрямую через kernel SQL над live tables.

    Используем market_filter='WHERE m.condition_id IN (...)' без is_active check
    — markets уже resolved но мы хотим их посчитать для парности.
    """
    from app.services.database import fetch

    cids_csv = ", ".join(f"'{c}'" for c in condition_ids)
    sql = build_features_sql(
        markets_src="markets",
        snaps_src="live_price_snapshots",
        buckets_src="live_trade_buckets",
        wflow_src="live_wallet_flow_per_market",
        anchor_h=anchor_h,
        market_filter=f"WHERE m.condition_id IN ({cids_csv})",
        is_duckdb=False,
    )
    rows = await fetch(sql)
    return {r["condition_id"]: dict(r) for r in rows}


def _diff_features(
    canonical: dict[str, dict],
    live: dict[str, dict],
) -> list[tuple[str, str, float, float]]:
    """Returns list of (cid, col, canonical_val, live_val) where diff > TOLERANCE."""
    diffs: list[tuple[str, str, float, float]] = []
    common_cids = set(canonical.keys()) & set(live.keys())
    for cid in sorted(common_cids):
        c_row = canonical[cid]
        l_row = live[cid]
        for col in FEATURE_COLUMNS:
            if col in SKIP_COLUMNS:
                continue
            c_val = c_row.get(col)
            l_val = l_row.get(col)
            # NULL on both sides → equal
            if c_val is None and l_val is None:
                continue
            # NULL on one side → diff
            if c_val is None or l_val is None:
                diffs.append((cid, col, c_val, l_val))
                continue
            try:
                cf = float(c_val)
                lf = float(l_val)
                if abs(cf - lf) > TOLERANCE:
                    diffs.append((cid, col, cf, lf))
            except (TypeError, ValueError):
                # Non-numeric (str, bool) — strict equality
                if c_val != l_val:
                    diffs.append((cid, col, c_val, l_val))
    return diffs


async def main() -> int:
    log.info("Feature parity check started (anchor=%dh, N=%d)", ANCHOR_FOR_CHECK, N_MARKETS_TO_CHECK)

    cids = await _fetch_recent_resolved_markets()
    if not cids:
        log.warning("No recently resolved markets found — nothing to check.")
        return 0

    log.info("Checking %d markets: %s", len(cids), [c[:12] + "..." for c in cids])

    canonical = _read_canonical_features(cids, ANCHOR_FOR_CHECK)
    if not canonical:
        log.warning("No canonical features for these markets (parquet may be stale or markets not in it)")
        return 0

    live = await _read_live_features(cids, ANCHOR_FOR_CHECK)
    if not live:
        log.warning("No live features for these markets (live tables may be empty)")
        return 0

    diffs = _diff_features(canonical, live)
    if not diffs:
        log.info("✓ Parity check PASSED — 0 diffs across %d markets × %d cols",
                 len(set(canonical.keys()) & set(live.keys())),
                 len([c for c in FEATURE_COLUMNS if c not in SKIP_COLUMNS]))
        return 0

    # Diffs found
    msg_lines = [f"⚠ Feature parity DRIFT detected ({len(diffs)} diffs):"]
    for cid, col, cv, lv in diffs[:20]:
        msg_lines.append(f"  {cid[:10]}... {col}: canonical={cv} live={lv}")
    if len(diffs) > 20:
        msg_lines.append(f"  ... and {len(diffs) - 20} more")
    msg = "\n".join(msg_lines)
    log.error(msg)
    await _send_tg_alert(msg)
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
