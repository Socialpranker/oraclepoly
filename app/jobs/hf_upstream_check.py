"""HF upstream check — daily 06:00 UTC.

Phoenix V2 (2026-04-30): HF dataset SII-WANGZJ/Polymarket_data — primary source
для backtest/calibration. Обновляется irregularly (last: 2026-03-31).

Что делает cron:
1. Запрашивает HF API last_modified для quant.parquet
2. Сравнивает с локальным mtime /mnt/data/hf_data/quant.parquet
3. Если HF новее → log alert (без auto-download — file 36GB, опасно качать в фоне)
4. Записывает status в БД (etl_runs table) для мониторинга

Реакция на alert:
- TG message admin: "HF dataset updated to <date>, queue manual download"
- Manual: `python3 scripts/download_hf_quant.py` (отдельный script с --confirm)
- После download: cron rebuild_enriched + L1+L2 refit

Без auto-download потому что:
- 36GB файл, на 79% disk это рисково
- Новая версия может ломать схему (надо инспектировать)
- Lab не должен внезапно перетренироваться без человеческого approval
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()

QUANT_LOCAL = "/mnt/data/hf_data/quant.parquet"
HF_REPO = "SII-WANGZJ/Polymarket_data"
HF_FILENAME = "quant.parquet"


async def run_hf_upstream_check() -> dict[str, Any]:
    """Check if HF upstream has newer quant.parquet.

    Returns: {
        local_mtime, upstream_mtime, is_stale,
        days_since_local, days_since_upstream
    }
    """
    try:
        from huggingface_hub import HfApi
    except ImportError:
        logger.warning("hf_check_skipped", reason="huggingface_hub not installed")
        return {"status": "skipped", "reason": "missing_lib"}

    local_path = Path(QUANT_LOCAL)
    if not local_path.exists():
        logger.error("hf_check_no_local", path=QUANT_LOCAL)
        return {"status": "error", "reason": "no_local_file"}

    local_mtime = datetime.fromtimestamp(local_path.stat().st_mtime, tz=UTC)
    now = datetime.now(UTC)
    days_since_local = (now - local_mtime).days

    try:
        api = HfApi()
        info = api.dataset_info(HF_REPO)
        upstream_mtime = info.last_modified
        if upstream_mtime is None:
            logger.warning("hf_check_no_upstream_mtime")
            return {"status": "error", "reason": "no_upstream_mtime"}
    except Exception as exc:
        logger.warning("hf_check_api_failed", error=str(exc))
        return {"status": "error", "reason": "api_failed", "error": str(exc)}

    days_since_upstream = (now - upstream_mtime).days
    is_stale = upstream_mtime > local_mtime

    result = {
        "status": "ok",
        "local_mtime": local_mtime.isoformat(),
        "upstream_mtime": upstream_mtime.isoformat(),
        "is_stale": is_stale,
        "days_since_local": days_since_local,
        "days_since_upstream": days_since_upstream,
    }

    if is_stale:
        logger.warning(
            "hf_dataset_stale",
            local=local_mtime.isoformat(),
            upstream=upstream_mtime.isoformat(),
            gap_days=days_since_local - days_since_upstream,
        )
        # TG alert (если configured)
        await _maybe_alert_tg(local_mtime, upstream_mtime)
    else:
        logger.info(
            "hf_dataset_current",
            local=local_mtime.isoformat(),
            upstream=upstream_mtime.isoformat(),
        )

    return result


async def _maybe_alert_tg(local_mtime: datetime, upstream_mtime: datetime) -> None:
    """TG alert если upstream новее. Не блокирует cron при ошибке."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    admin_ids = os.getenv("ADMIN_TELEGRAM_IDS", "").split(",")
    if not bot_token or not admin_ids or not admin_ids[0]:
        return

    try:
        import httpx

        gap_days = (upstream_mtime - local_mtime).days
        text = (
            f"📊 *HF dataset обновлён*\n\n"
            f"Local: `{local_mtime.strftime('%Y-%m-%d')}`\n"
            f"Upstream: `{upstream_mtime.strftime('%Y-%m-%d')}`\n"
            f"Gap: *{gap_days} дн.*\n\n"
            f"Run: `python3 scripts/download_hf_quant.py --confirm`"
        )
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        async with httpx.AsyncClient(timeout=10.0) as client:
            for chat_id in admin_ids:
                if not chat_id.strip():
                    continue
                await client.post(
                    url,
                    json={
                        "chat_id": chat_id.strip(),
                        "text": text,
                        "parse_mode": "Markdown",
                    },
                )
    except Exception as exc:
        logger.warning("hf_check_tg_alert_failed", error=str(exc))
