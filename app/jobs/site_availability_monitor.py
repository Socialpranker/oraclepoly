"""site-availability job — проверка доступности публичного сайта снаружи nginx.

Инцидент 2026-07-29: nginx не поднялся после рестарта ([emerg] host not found in
upstream — литеральный хост в proxy_pass резолвится на старте). Хостовый nginx лежал
6 суток, 80/443 никто не слушал, Telegram Mini App получал обрыв TLS. Docker-контейнеры
всё это время были healthy, поэтому НИ ОДИН существующий мониторинг ничего не заметил —
все они смотрят внутрь стека. Поломку нашёл человек.

Джоба ходит на публичный URL тем же путём, что и пользователь (DNS → внешний IP →
nginx → docker), поэтому ловит именно тот слой, который у всех остальных проверок
слепое пятно: TLS-терминацию и реверс-прокси.

Осознанное ограничение: джоба живёт внутри того же сервера. Смерть всего хоста или
docker'а она не заметит — молчит вместе с ними. Это страхует внешний пинг, не эта джоба.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

logger = structlog.get_logger()

# Публичный адрес, а не localhost: цель — проверить весь путь целиком, включая
# DNS, TLS и nginx. Проверка через localhost:3000 прошла бы и во время инцидента.
SITE_URL = "https://example.com"

REQUEST_TIMEOUT = 20.0

# Мини-апп грузится из index.html; пустой или обрезанный ответ = белый экран у
# пользователя, даже если код 200. Реальный размер ~1.7 КБ.
MIN_HTML_BYTES = 500

_ALERT_STATE_PREFIX = "site:alert_state:"


class ProbeResult:
    """Итог одной проверки: ok + человекочитаемая причина отказа."""

    def __init__(self, name: str, ok: bool, detail: str) -> None:
        self.name = name
        self.ok = ok
        self.detail = detail


async def _probe_frontend(client: httpx.AsyncClient) -> ProbeResult:
    """GET / — 200 и непустой HTML."""
    try:
        response = await client.get(f"{SITE_URL}/")
    except Exception as exc:
        return ProbeResult("frontend", False, f"запрос не прошёл: {type(exc).__name__}: {str(exc)[:150]}")

    if response.status_code != 200:
        return ProbeResult("frontend", False, f"код ответа {response.status_code}, ожидался 200")

    size = len(response.content)
    if size < MIN_HTML_BYTES:
        return ProbeResult("frontend", False, f"HTML подозрительно мал: {size} байт (порог {MIN_HTML_BYTES})")

    return ProbeResult("frontend", True, f"200, {size} байт")


async def _probe_api(client: httpx.AsyncClient) -> ProbeResult:
    """GET /health — 200 от backend через nginx.

    Путь именно /health, не /api/health: последнего не существует и на самом
    backend, проверка по нему всегда была бы красной.
    """
    try:
        response = await client.get(f"{SITE_URL}/health")
    except Exception as exc:
        return ProbeResult("api", False, f"запрос не прошёл: {type(exc).__name__}: {str(exc)[:150]}")

    if response.status_code != 200:
        return ProbeResult("api", False, f"код ответа {response.status_code}, ожидался 200")

    return ProbeResult("api", True, "200")


async def _alert_on_edge(condition: str, failing: bool, fail_msg: str, ok_msg: str) -> str | None:
    """Вернуть текст алерта ТОЛЬКО при смене состояния, иначе None.

    Тот же принцип, что в proxy_health_check._alert_on_edge: дедуп по событию, а не
    по времени. Ключ без TTL — протухание при всё ещё лежащем сайте выглядело бы как
    новая поломка и вернуло бы спам.

    Redis недоступен → шлём поломку (сигнал важнее дедупа), но не шлём recovery:
    без прошлого состояния «восстановление» — не событие.
    """
    key = f"{_ALERT_STATE_PREFIX}{condition}"
    new_state = "fail" if failing else "ok"

    try:
        from app.core.cache import get_redis

        redis = await get_redis()
    except Exception as exc:
        logger.warning("site_alert_state_redis_unavailable", condition=condition, error=str(exc))
        redis = None

    if not redis:
        return fail_msg if failing else None

    try:
        raw = await redis.get(key)
    except Exception as exc:
        logger.warning("site_alert_state_read_failed", condition=condition, error=str(exc))
        return fail_msg if failing else None

    prev_state = raw.decode() if isinstance(raw, bytes) else raw

    if prev_state == new_state:
        return None

    try:
        await redis.set(key, new_state)
    except Exception as exc:
        logger.warning("site_alert_state_write_failed", condition=condition, error=str(exc))

    if failing:
        return fail_msg
    # Первый в истории прогон на здоровом сайте — не событие, молчим.
    if prev_state is None:
        return None
    return ok_msg


async def run_site_availability_monitor() -> dict[str, Any]:
    """Проверить публичную доступность сайта, при смене состояния — алерт админу."""
    # follow_redirects=False намеренно: / должен отдавать 200 напрямую. Молчаливый
    # редирект на заглушку хостера прошёл бы проверку и скрыл поломку.
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=False) as client:
        probes = [await _probe_frontend(client), await _probe_api(client)]

    failed = [p for p in probes if not p.ok]
    checks = {p.name: {"ok": p.ok, "detail": p.detail} for p in probes}

    if failed:
        logger.warning(
            "site_availability_failed",
            failed=[p.name for p in failed],
            checks=checks,
        )
    else:
        logger.info("site_availability_ok", checks=checks)

    alerts: list[str] = []
    for probe in probes:
        what = "Фронт" if probe.name == "frontend" else "API"
        fail_msg = (
            f"🔴 <b>{what} недоступен снаружи</b>\n\n"
            f"{SITE_URL} — {probe.detail}\n\n"
            f"Мини-апп в Telegram сейчас не открывается.\n"
            f"Проверить в первую очередь: <code>systemctl status nginx</code> на проде — "
            f"именно он лёг в инциденте 29 июля."
        )
        ok_msg = f"✅ <b>{what} снова отвечает</b>\n\n{SITE_URL} — {probe.detail}"

        message = await _alert_on_edge(probe.name, failing=not probe.ok, fail_msg=fail_msg, ok_msg=ok_msg)
        if message:
            alerts.append(message)

    if alerts:
        try:
            from app.tasks.notifications import send_admin_message

            for message in alerts:
                await send_admin_message(message)
        except Exception as exc:
            # Алерт не ушёл — но джоба обязана вернуть результат, а не упасть.
            logger.warning("site_availability_alert_send_failed", error=str(exc))

    return {
        "ok": not failed,
        "checks": checks,
        "alerts_sent": len(alerts),
    }
