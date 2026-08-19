"""Polymarket API health monitor.

Читает Redis counter `polymarket:api:errors:{YYYYMMDDHH}:{status}` (ставится
из app.core.retry._record_polymarket_error при каждой retryable ошибке).
Сравнивает кол-во 429/5xx за прошедший час с baseline (moving avg 7 дней).
Алёртит в TG если error_rate сильно вырос (default threshold 3x).

Дополнительно (audit second-tier): проверяет freshness WS market-канала —
polymarket_ws_service пишет "ws:last_message_at" при каждом полученном
сообщении (throttled раз в 5с, см. PolymarketWebSocketService._record_heartbeat).
Молчание > 2 мин — сигнал half-open/stall соединения, которое протокольный
ping/pong не всегда ловит мгновенно.

Cron: hourly (e.g. :05 минут каждого часа, после polymarket-poller).

Дизайн: НЕ читаем "total requests" — мы не считаем success'ы, только errors.
Threshold strategy: алёрт если errors_last_hour > max(MIN_ERRORS_ABS,
baseline * ALERT_RATIO). MIN_ERRORS_ABS защищает от спама когда baseline
очень низкий и одна ошибка превышает ratio.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from app.core.cache import cache_get, get_redis
from app.core.config import get_settings
from app.core.task_queue import debounce_acquire

logger = structlog.get_logger(__name__)

# WS heartbeat freshness: молчание дольше этого — подозрение на half-open/stall.
WS_STALE_THRESHOLD_SEC = 120

# Debounce для WS-молчания алёрта: не долбить чаще раза в 30 минут.
WS_ALERT_DEBOUNCE_SEC = 1800
WS_ALERT_DEBOUNCE_KEY = "alert:ws_stale"

# Debounce для error-rate алёрта. Cron гоняет каждый час, а baseline (moving avg)
# сам подмешивает уже-повышенные часы — ratio-threshold растёт вместе с error_rate
# и should_alert остаётся True час за часом на одном и том же инциденте
# (инцидент 2026-08-11: 4 TG-алерта подряд за вечер, 17:05-22:05). 3 часа даёт
# переалёрт на затянувшейся деградации, но глушит повторный триггер на соседнем часе.
API_ERROR_ALERT_DEBOUNCE_SEC = 3 * 3600
API_ERROR_ALERT_DEBOUNCE_KEY = "alert:api_errors_degraded"

# Минимальный absolute threshold чтобы избежать алёртов на единичные ошибки.
# Polymarket /trades регулярно даёт 429 (мы throttle на concurrency=2),
# 5-10 ошибок за час норма.
MIN_ERRORS_ABS = 20

# Алёрт если errors_last_hour >= baseline * ALERT_RATIO.
# 3.0 = тройной рост — статистически значимый, не шум.
ALERT_RATIO = 3.0

# Окно для baseline (часов). 7 дней = 168 часов даёт устойчивый средний.
BASELINE_WINDOW_HOURS = 168

# Минимум часов с данными для надёжного baseline. Если данных меньше —
# используем MIN_ERRORS_ABS как абсолютный threshold (no ratio check).
MIN_BASELINE_HOURS = 24


def _hour_bucket(dt: datetime) -> str:
    """Format datetime as YYYYMMDDHH для Redis key."""
    return dt.strftime("%Y%m%d%H")


async def _fetch_errors_for_hour(redis: Any, bucket: str) -> dict[int, int]:
    """Read all status counters for one hour bucket. Returns {status: count}."""
    pattern = f"polymarket:api:errors:{bucket}:*"
    keys = []
    async for key in redis.scan_iter(match=pattern, count=100):
        keys.append(key)
    if not keys:
        return {}
    values = await redis.mget(*keys)
    result: dict[int, int] = {}
    for k, v in zip(keys, values, strict=False):
        if v is None:
            continue
        key_str = k.decode() if isinstance(k, bytes) else k
        status = int(key_str.rsplit(":", 1)[-1])
        result[status] = int(v)
    return result


async def _compute_baseline(redis: Any, now: datetime) -> tuple[float, int]:
    """Compute average errors-per-hour across BASELINE_WINDOW_HOURS, excluding last hour.

    Returns (avg_errors_per_hour, n_hours_with_data).
    """
    total_errors = 0
    hours_seen = 0
    # Start from 2 hours ago (skip current hour and last completed hour to avoid
    # contamination with the hour being evaluated).
    for hours_back in range(2, BASELINE_WINDOW_HOURS + 2):
        bucket = _hour_bucket(now - timedelta(hours=hours_back))
        errors = await _fetch_errors_for_hour(redis, bucket)
        if errors:
            total_errors += sum(errors.values())
            hours_seen += 1

    avg = total_errors / hours_seen if hours_seen > 0 else 0.0
    return avg, hours_seen


async def _check_ws_freshness() -> dict[str, Any]:
    """Проверить свежесть WS market-канала через "ws:last_message_at" (audit
    second-tier). Молчание > WS_STALE_THRESHOLD_SEC → TG-алерт, debounce 30 мин.

    Отсутствие ключа (WS ни разу не писал heartbeat — сервис только стартовал
    или heartbeat вообще не настроен) НЕ алёртится: недостаточно данных для
    вывода "молчит", не хотим ложных срабатываний на cold-start.
    """
    hb = await cache_get("ws:last_message_at")
    if not hb or "ts" not in hb:
        logger.info("ws_freshness_no_heartbeat_yet")
        return {"status": "no_data"}

    age = time.time() - float(hb["ts"])
    if age <= WS_STALE_THRESHOLD_SEC:
        return {"status": "ok", "age_sec": round(age, 1)}

    logger.warning("ws_freshness_stale", age_sec=round(age, 1))

    if not await debounce_acquire(WS_ALERT_DEBOUNCE_KEY, WS_ALERT_DEBOUNCE_SEC):
        return {"status": "stale_debounced", "age_sec": round(age, 1)}

    from app.tasks.notifications import send_admin_message

    await send_admin_message(f"⚠️ Polymarket WS молчит >2 мин (последнее сообщение {round(age)}с назад)")
    return {"status": "stale_alerted", "age_sec": round(age, 1)}


def _format_alert(
    last_hour_errors: dict[int, int],
    baseline_avg: float,
    n_baseline_hours: int,
    threshold_used: float,
) -> str:
    """Pure formatter для testability."""
    total_last = sum(last_hour_errors.values())
    breakdown_lines = [f"  HTTP {status}: <b>{count}</b>" for status, count in sorted(last_hour_errors.items())]

    lines = [
        "🌐 <b>Polymarket API Degraded</b>",
        "",
        f"Errors за last hour: <b>{total_last}</b>",
        *breakdown_lines,
        "",
        f"Baseline (avg/hour за {n_baseline_hours}h): {baseline_avg:.1f}",
        f"Threshold (alert at): {threshold_used:.1f}",
        "",
        "Возможные причины:",
        "• Polymarket снизил rate-limit",
        "• Какой-то детектор стал aggressive (D7b/D13/D14 fetch /trades)",
        "• Polymarket maintenance / outage",
        "",
        "Действия:",
        "• <code>ssh your-server 'docker logs --since 1h oraclepoly-backend | grep -c \"429 Too Many\"'</code>",
        "• Если виноват один детектор — уменьши его cron frequency",
    ]
    return "\n".join(lines)


async def run_polymarket_api_health_check() -> dict[str, Any]:
    """Один проход monitor'а. Возвращает stats + alert state."""
    redis = await get_redis()
    if not redis:
        logger.warning("polymarket_api_health_no_redis")
        return {"status": "no_redis"}

    # WS freshness — независимая проверка (audit second-tier), не блокирует
    # остальную логику error-rate мониторинга при сбое.
    try:
        ws_freshness = await _check_ws_freshness()
    except Exception:
        logger.exception("ws_freshness_check_failed")
        ws_freshness = {"status": "check_failed"}

    now = datetime.now(UTC)
    # "last hour" = прошедший полный час (now - 1h). Текущий неполный — пропускаем,
    # иначе baseline и текущая не сравнимы.
    last_hour_bucket = _hour_bucket(now - timedelta(hours=1))
    last_hour_errors = await _fetch_errors_for_hour(redis, last_hour_bucket)
    total_last = sum(last_hour_errors.values())

    baseline_avg, n_baseline_hours = await _compute_baseline(redis, now)

    # Choose threshold based on baseline data availability.
    if n_baseline_hours < MIN_BASELINE_HOURS:
        threshold = float(MIN_ERRORS_ABS)
        reason = "baseline_insufficient"
    else:
        threshold = max(MIN_ERRORS_ABS, baseline_avg * ALERT_RATIO)
        reason = "ratio_check"

    should_alert = total_last >= threshold and total_last >= MIN_ERRORS_ABS

    logger.info(
        "polymarket_api_health_checked",
        last_hour_total=total_last,
        baseline_avg=round(baseline_avg, 2),
        n_baseline_hours=n_baseline_hours,
        threshold=round(threshold, 2),
        reason=reason,
        should_alert=should_alert,
    )

    if not should_alert:
        return {
            "status": "ok",
            "last_hour_total": total_last,
            "baseline_avg": round(baseline_avg, 2),
            "threshold": round(threshold, 2),
            "ws_freshness": ws_freshness,
        }

    if not await debounce_acquire(API_ERROR_ALERT_DEBOUNCE_KEY, API_ERROR_ALERT_DEBOUNCE_SEC):
        logger.info("polymarket_api_health_alert_debounced", last_hour_total=total_last)
        return {
            "status": "alert_debounced",
            "last_hour_total": total_last,
            "last_hour_breakdown": last_hour_errors,
            "baseline_avg": round(baseline_avg, 2),
            "threshold": round(threshold, 2),
            "ws_freshness": ws_freshness,
        }

    text = _format_alert(last_hour_errors, baseline_avg, n_baseline_hours, threshold)
    settings = get_settings()
    from app.api.telegram import send_telegram_message

    for admin_id in settings.admin_telegram_ids:
        try:
            await send_telegram_message(admin_id, text)
        except Exception:
            logger.exception("polymarket_api_health_telegram_failed", admin_id=admin_id)

    return {
        "status": "alert",
        "last_hour_total": total_last,
        "last_hour_breakdown": last_hour_errors,
        "baseline_avg": round(baseline_avg, 2),
        "threshold": round(threshold, 2),
        "ws_freshness": ws_freshness,
    }


logging.basicConfig(level=logging.INFO)
