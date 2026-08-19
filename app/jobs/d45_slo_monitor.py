"""D45 SLO monitor — детектирует проблемы D45 стека и шлёт TG alert.

В отличие от `detector_slo_monitor` (который проверяет emission rate против
baseline), D45 в Shadow mode имеет volatile signal-rate (зависит от market
divergence) — baseline emission не годится.

Архитектурная заметка:
    Stream services живут в backend process (FastAPI lifespan). Cron job
    запускается в отдельном `docker exec` процессе и НЕ имеет доступа к
    их in-memory state. Поэтому monitor дёргает internal HTTP endpoint
    `/api/d45/stats` (admin-only) через localhost, парсит результат, шлёт TG
    при проблемах.

Cron: hourly. Silent если OK (info-log only), TG-alert при проблемах.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from app.analytics.notification import Notification, Severity
from app.core.config import get_settings
from app.services.notification_bus import publish

logger = structlog.get_logger(__name__)

# Threshold для "свежего" tick'а в seconds.
FRESH_TICK_THRESHOLD_S = 300.0  # 5 min — много для 440ms cadence Pyth

# Cron exec'ится внутри backend container, где uvicorn слушает 8000
# (см. Dockerfile: --port 8000). Локальный dev-стек использует 8001
# через start.sh, но на проде это 8000.
STATS_URL = "http://127.0.0.1:8000/api/d45/stats"
HTTP_TIMEOUT_S = 10.0


def to_notifications(issues: list[str], *, backend_unreachable: bool = False) -> list[Notification]:
    """Проблемы стека D45 → Notification (спека §3).

    what_if_ignored=None: D45 в shadow, реальных денег на нём нет — цену
    бездействия пришлось бы выдумать, а §3 это запрещает. Вместо цифры —
    качественное последствие (детектор слепнет, статистика копится ложная).

    Технические команды диагностики уходят в evidence, а не в тело: владельцу
    нужен вывод, отладочные строки — справочно (спека §5).
    """
    if backend_unreachable:
        return [
            Notification(
                severity=Severity.ACTION_REQUIRED,
                subject="infra:d45_stream",
                kind="d45_slo",
                headline="Не видно, жив ли поток котировок D45",
                what_happened=[
                    "Монитор не смог получить статистику потока: backend не отвечает либо не задан ключ доступа.",
                    "Пока статистика недоступна, о самом потоке ничего не известно — он может быть и жив, и мёртв.",
                ],
                what_if_ignored=None,
                what_to_do="Проверь, поднят ли backend-контейнер, и что ADMIN_API_KEY задан в окружении.",
                actions=[],
                evidence=[f"GET {STATS_URL} не ответил"],
            )
        ]

    if not issues:
        return []

    return [
        Notification(
            severity=Severity.ACTION_REQUIRED,
            subject="infra:d45_stream",
            kind="d45_slo",
            headline="Поток котировок D45 работает не полностью",
            what_happened=[
                *issues,
                "Детектор сравнивает цены двух площадок — без свежих котировок "
                "он не находит расхождений и молча копит пустую статистику.",
            ],
            what_if_ignored=None,
            what_to_do=(
                "Посмотри логи backend за последние 10 минут по pyth/binance/d45 — "
                "обычно это обрыв соединения, который лечится перезапуском контейнера."
            ),
            actions=[],
            evidence=[
                "docker logs --since 10m oraclepoly-backend | grep -E 'pyth|binance|d45'",
                f"curl {STATS_URL} -H 'Authorization: Bearer $ADMIN_API_KEY'",
            ],
        )
    ]


async def _fetch_stats() -> dict[str, Any] | None:
    """GET /api/d45/stats. Возвращает None если HTTP failed."""
    settings = get_settings()
    api_key = settings.admin_api_key
    if not api_key:
        logger.warning("d45_slo_no_admin_api_key")
        return None

    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
            resp = await client.get(STATS_URL, headers=headers)
            resp.raise_for_status()
            return resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("d45_slo_stats_fetch_failed", error=str(exc))
        return None


def _detect_issues(stats: dict[str, Any]) -> list[str]:
    """Извлекаем проблемы из stats response. Pure function для тестируемости."""
    issues: list[str] = []

    if not stats.get("enabled"):
        # D45 disabled — не считаем за issue, skip-handling делается выше.
        return issues

    pyth = stats.get("pyth") or {}
    binance = stats.get("binance") or {}

    if not pyth.get("connected"):
        issues.append("Pyth SSE disconnected")
    if not binance.get("connected"):
        issues.append("Binance WS disconnected")

    # Per-symbol freshness — главный symbol BTC/USD
    for label, src in [("Pyth", pyth), ("Binance", binance)]:
        symbols = src.get("symbols") or {}
        btc = symbols.get("BTC/USD") or {}
        if not btc.get("fresh"):
            issues.append(f"{label} BTC/USD: нет свежих ticks (cache empty или stale)")
        else:
            age_s = btc.get("age_s")
            if isinstance(age_s, int | float) and age_s > FRESH_TICK_THRESHOLD_S:
                issues.append(f"{label} BTC/USD: последний tick {age_s:.0f}s назад")

    return issues


async def run_d45_slo_check() -> dict[str, Any]:
    """Один проход monitor'а.

    Returns:
        {'status': 'ok'|'alert'|'skipped', 'issues': [...]}
    """
    settings = get_settings()
    if not settings.d45_chainlink_leadlag_enabled:
        logger.info("d45_slo_skipped_disabled")
        return {"status": "skipped", "reason": "feature_flag_disabled"}

    stats = await _fetch_stats()
    if stats is None:
        # HTTP failed — это сама по себе проблема (backend down?)
        issues = ["Не удалось получить /api/d45/stats (backend down или admin_api_key missing)"]
        await publish(to_notifications([], backend_unreachable=True), actor="d45_slo_monitor")
        return {"status": "alert", "issues": issues}

    issues = _detect_issues(stats)

    if issues:
        await publish(to_notifications(issues), actor="d45_slo_monitor")
        logger.warning("d45_slo_alert_sent", n_issues=len(issues), issues=issues)
        return {"status": "alert", "issues": issues}

    logger.info("d45_slo_ok")
    return {"status": "ok", "issues": []}
