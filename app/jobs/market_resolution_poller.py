"""Market resolution poller — demand-driven резолв рынков с открытыми позициями.

Root cause инцидента 2026-07-07 (6 live-позиций D3 зависли по 1.5-5.5 дней):
bulk-каналы резолва не гарантируют покрытие наших позиций.
- market_sync тянет только 1500 последних closed events (order=endDate desc) —
  при ~63K закрытий/нед рынок выпадает из окна за часы и его resolved/winner
  больше никогда не обновляются;
- все 3 sweep'а resolve_outcomes идут через INNER JOIN oracle_snapshots —
  рынок без whale-снапшотов структурно невидим для safety-net.

Этот poller инвертирует направление: вместо сканирования firehose закрытых
рынков перечисляет рынки НАШИХ открытых ордеров (O(позиций), сейчас ~30,
не O(рынков Polymarket)) и опрашивает CLOB API точечно.
GET /markets/{condition_id} отдаёт явный tokens[].winner — авторитетный флаг
резолва, не эвристика по ценам. Нерезолвнутый рынок честно показывает оба
winner=false → ждём следующего тика (настоящая UMA-задержка обрабатывается
сама собой).

Дальше ничего не меняем: v2_execution_resolve._settle_filled() подхватывает
по своему гейту (m.resolved = true AND m.winner IN ('yes','no')), за ним
redeem_job.

Read-only вызов → напрямую, без POLYMARKET_PROXY (прокси только для ордеров).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog

from app.analytics.notification import Notification, Severity
from app.core.config import get_settings
from app.core.task_queue import debounce_acquire
from app.services.database import execute, fetch
from app.services.notification_bus import publish

logger = structlog.get_logger(__name__)

# Bounded concurrency для fetch-фазы — не резолв (тот строго последовательный).
_FETCH_CONCURRENCY = 8

# Рынки открытых ордеров (live + virtual), которые пора резолвить.
# yes/no token_id — для точного матча winner-токена CLOB-ответа (порядок
# clob_token_ids: [yes, no], та же конвенция что в v2_execution_resolve).
_DEMAND_SQL = """
SELECT DISTINCT m.id::text            AS internal_id,
       m.polymarket_id,
       m.condition_id,
       m.clob_token_ids->>0           AS yes_token_id,
       m.clob_token_ids->>1           AS no_token_id,
       m.end_date,
       m.question
  FROM execution_orders eo
  JOIN markets m ON m.polymarket_id = eo.market_id
 WHERE eo.status IN ('placed', 'filled', 'virtual_filled')
   AND m.resolved = false
   AND m.condition_id IS NOT NULL
   AND (m.end_date < now() OR m.is_active = false)
 ORDER BY m.end_date
 LIMIT 200
"""

# Рынок закрыт дольше этого срока и всё ещё не резолвнут → TG-алерт
# (настоящий UMA dispute / деградация API перестают быть невидимыми).
_STUCK_THRESHOLD = timedelta(days=3)
_STUCK_ALERT_DEBOUNCE_KEY = "market_resolution_poller:stuck_alert"
_STUCK_ALERT_DEBOUNCE_TTL = 86400  # раз в сутки


async def _fetch_clob_market(client: httpx.AsyncClient, condition_id: str) -> dict[str, Any] | None:
    """GET CLOB /markets/{condition_id}. None при любой HTTP-ошибке (retry следующим тиком).

    client переиспользуется всем прогоном (см. run_market_resolution_poll) —
    без нового TCP+TLS handshake на каждый рынок.
    """
    url = f"{get_settings().polymarket_host}/markets/{condition_id}"
    try:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.warning("clob_market_fetch_failed", condition_id=condition_id, error=str(e)[:200])
        return None


async def _fetch_all_clob_markets(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any] | None]:
    """Fetch-фаза: один AsyncClient на весь прогон + Semaphore(8) bounded concurrency.

    Ошибка одного рынка не валит остальные — _fetch_clob_market сам глотает
    исключения и возвращает None, здесь дополнительно return_exceptions=True
    как defensive backstop.
    """
    semaphore = asyncio.Semaphore(_FETCH_CONCURRENCY)

    async def _bounded_fetch(client: httpx.AsyncClient, condition_id: str) -> dict[str, Any] | None:
        async with semaphore:
            return await _fetch_clob_market(client, condition_id)

    async with httpx.AsyncClient(timeout=10.0) as client:
        results = await asyncio.gather(
            *(_bounded_fetch(client, row["condition_id"]) for row in rows),
            return_exceptions=True,
        )

    clob_markets: dict[str, dict[str, Any] | None] = {}
    for row, result in zip(rows, results, strict=True):
        clob_markets[row["condition_id"]] = None if isinstance(result, BaseException) else result
    return clob_markets


def _extract_winner(clob_market: dict[str, Any], yes_token_id: str | None, no_token_id: str | None) -> str | None:
    """tokens[].winner → 'yes'/'no'. None = не резолвнут или ответ не поддаётся однозначной трактовке.

    Матчим по token_id, не по позиции в массиве и не по имени outcome
    (у спорта outcome = названия команд) — с деньгами не гадаем.
    """
    tokens = clob_market.get("tokens") or []
    winners = [t for t in tokens if t.get("winner")]
    if len(winners) != 1:
        return None

    winner_token_id = str(winners[0].get("token_id", ""))
    if yes_token_id and winner_token_id == str(yes_token_id):
        return "yes"
    if no_token_id and winner_token_id == str(no_token_id):
        return "no"

    logger.warning(
        "clob_winner_token_mismatch",
        winner_token_id=winner_token_id,
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
    )
    return None


def to_notifications(stuck: list[dict[str, Any]]) -> list[Notification]:
    """Зависшие резолвы → Notification (спека §3).

    Одно уведомление на всю пачку, а не по одному на рынок: причина у них общая
    (резолв-пайплайн не доезжает), действие тоже одно. Здесь это оправдано, в
    отличие от зависших позиций, где у каждой свои деньги и свой subject.

    what_if_ignored=None намеренно: открытые позиции на этих рынках есть, но во
    что они обойдутся — неизвестно, исход не определён. Придумывать цифру §3
    запрещает.
    """
    if not stuck:
        return []

    now = datetime.now(UTC)
    lines = [f"{len(stuck)} рынков с открытыми позициями не резолвнуты больше трёх дней после закрытия."]
    for row in stuck[:10]:
        days = (now - row["end_date"]).days
        lines.append(f"• {row['question'][:80]} — {days}д после end_date")
    if len(stuck) > 10:
        lines.append(f"…и ещё {len(stuck) - 10}.")

    return [
        Notification(
            severity=Severity.ACTION_REQUIRED,
            subject="infra:market_resolution",
            kind="resolution_stuck",
            headline="Резолв рынков завис больше чем на три дня",
            what_happened=lines,
            what_if_ignored=None,
            what_to_do=(
                "Проверь, отдаёт ли CLOB winner по этим рынкам: если отдаёт — резолв-пайплайн "
                "не доезжает, если нет — рынок закрыт без исхода и ждёт разрешения на Polymarket."
            ),
            actions=[],
            evidence=[f"stuck_markets={len(stuck)}"],
        )
    ]


async def _notify_stuck(stuck: list[dict[str, Any]]) -> None:
    """TG-алерт по рынкам, зависшим нерезолвнутыми >3 дней. Debounce — раз в сутки.

    Свой debounce остаётся (решение владельца по очереди 1): джоба сама решает,
    слать ли, и зовёт шину только когда решила да.
    """
    if not await debounce_acquire(_STUCK_ALERT_DEBOUNCE_KEY, _STUCK_ALERT_DEBOUNCE_TTL):
        return

    await publish(to_notifications(stuck), actor="market_resolution_poller")


async def run_market_resolution_poll() -> dict[str, Any]:
    """Entry point для cron_runner (market-resolution-poll)."""
    rows = await fetch(_DEMAND_SQL)
    if not rows:
        return {"status": "ok", "checked": 0, "resolved": 0, "pending": 0, "errors": 0}

    from app.tasks.resolve_outcomes import resolve_single_market

    resolved = 0
    pending = 0
    errors = 0
    stuck: list[dict[str, Any]] = []
    now = datetime.now(UTC)

    # Fetch-фаза параллельна (bounded), резолв найденных рынков — строго
    # последовательно (деньги, никакого concurrent write на один market).
    clob_markets = await _fetch_all_clob_markets(rows)

    for row in rows:
        clob_market = clob_markets[row["condition_id"]]
        if clob_market is None:
            errors += 1
            continue

        winner = _extract_winner(clob_market, row["yes_token_id"], row["no_token_id"])
        if winner is None:
            pending += 1
            end_date = row["end_date"]
            if end_date.tzinfo is None:
                end_date = end_date.replace(tzinfo=UTC)
            if now - end_date > _STUCK_THRESHOLD:
                stuck.append({**row, "end_date": end_date})
            continue

        await execute(
            "UPDATE markets SET resolved = true, winner = $1 WHERE id = $2",
            winner,
            row["internal_id"],
        )
        resolve_price = 1.0 if winner == "yes" else 0.0
        await resolve_single_market(row["internal_id"], winner, resolve_price)
        resolved += 1
        logger.info(
            "market_resolution_polled",
            polymarket_id=row["polymarket_id"],
            winner=winner,
            question=row["question"][:80],
        )

    if stuck:
        try:
            await _notify_stuck(stuck)
        except Exception as e:
            logger.warning("stuck_alert_failed", error=str(e)[:200])

    logger.info(
        "market_resolution_poll_complete",
        checked=len(rows),
        resolved=resolved,
        pending=pending,
        errors=errors,
    )
    return {"status": "ok", "checked": len(rows), "resolved": resolved, "pending": pending, "errors": errors}
