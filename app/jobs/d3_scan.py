"""D3 Scan — каждые N минут эмитит CrossMktArb сигналы по neg-risk events.

В отличие от D1 (event-driven, на whale TX), D3 работает на батче:
1. Загрузить TRUE neg-risk events (neg_risk=true, neg_risk_other=false, n_yes_winners=1)
2. Для каждого — собрать markets + текущие YES-цены
3. Прокинуть в d3_cross_mkt_arb.detect()
4. INSERT сигнала только если нет открытого сигнала на то же СОБЫТИЕ (app-level dedup)

Dedup — по event_id, не по market_id:
D3 — арбитраж на уровне события (sum YES prices > 1.20). Все маркеты
внутри neg-risk event коррелированы — резолвятся вместе. Один сигнал
на событие = один независимый обучающий пример для калибратора.
"""

from __future__ import annotations

from typing import Any

import structlog

from app.core.task_queue import enqueue
from app.detectors import d3_cross_mkt_arb
from app.detectors.d3_cross_mkt_arb import EventMarket
from app.detectors.token_resolver import no_token_id, yes_token_id
from app.repositories.detector_signal_repository import get_detector_signal_repository
from app.services.database import fetch, fetchval
from app.services.live_prices import get_live_yes_price

logger = structlog.get_logger()


_ACTIVE_NEGRISK_EVENTS_SQL = """
SELECT e.id           AS event_pk,
       e.polymarket_id AS event_polymarket_id,
       m.polymarket_id AS market_polymarket_id,
       m.condition_id,
       m.current_price,
       m.clob_token_ids
  FROM events e
  JOIN markets m ON m.event_id = e.id
 WHERE e.neg_risk = true
   AND e.neg_risk_other = false
   AND e.n_yes_winners = 1
   AND e.is_active = true
   AND m.is_active = true
   AND m.resolved = false
   AND m.current_price IS NOT NULL
   AND m.current_price > 0.01
   AND m.end_date IS NOT NULL
   AND m.end_date > now()
   AND m.end_date < now() + INTERVAL '30 days'
"""

# Один запрос на все события с открытыми D3-сигналами
_OPEN_D3_EVENTS_SQL = """
SELECT DISTINCT event_id
  FROM detector_signals
 WHERE detector = 'D3_cross_mkt_arb'
   AND resolved = false
   AND event_id IS NOT NULL
"""

# repo.insert() возвращает bool (True если вставлено), не id — Signal
# иммутабелен и не несёт DB-generated id (uuid gen_random_uuid()). Лукап по
# natural key сразу после insert: тот же ключ, которым миграция
# 20260430141632_detector_signals_table.sql обосновывает D3 idempotency
# ("uniqueness через generated_at + market_id"), здесь дополнен event_id
# и detector — однозначно адресует только что вставленную строку.
_FIND_JUST_INSERTED_SIGNAL_ID_SQL = """
SELECT id FROM detector_signals
 WHERE detector = $1 AND market_id = $2 AND event_id = $3 AND generated_at = $4
"""


async def _enqueue_for_execution(signal) -> None:
    """Резидентный execution-демон (Phase 3 Task 3, audit топ-7): после успешного
    insert кладём id сигнала в Redis-очередь, чтобы демон исполнил его за
    секунды вместо ожидания минутного execution-cron-тика. Best-effort —
    если id не нашёлся (гонка/реплика лагнула) или Redis недоступен, cron
    остаётся watchdog'ом и всё равно подберёт сигнал на следующем тике.
    """
    try:
        signal_id = await fetchval(
            _FIND_JUST_INSERTED_SIGNAL_ID_SQL,
            signal.detector,
            signal.market_id,
            signal.event_id,
            signal.generated_at,
        )
        if signal_id is None:
            logger.warning("d3_scan_enqueue_id_not_found", market=signal.market_id)
            return
        await enqueue("oracle:queue:execution_signals", {"detector_signal_id": str(signal_id)})
    except Exception as exc:
        logger.warning("d3_scan_enqueue_failed", error=str(exc), market=signal.market_id)


async def run_d3_scan() -> dict[str, Any]:
    rows = await fetch(_ACTIVE_NEGRISK_EVENTS_SQL)
    if not rows:
        logger.info("d3_scan_no_events")
        return {"events": 0, "markets": 0, "signals": 0, "inserted": 0, "skipped_open": 0}

    # События с уже открытым сигналом — пропускаем целиком
    open_rows = await fetch(_OPEN_D3_EVENTS_SQL)
    open_events: set[str] = {r["event_id"] for r in open_rows}

    by_event: dict[str, list[EventMarket]] = {}
    n_live_priced = 0
    for r in rows:
        event_pid = r["event_polymarket_id"]
        db_price = float(r["current_price"])
        live_price, is_live = await get_live_yes_price(yes_token_id(r.get("clob_token_ids")), db_price)
        if is_live:
            n_live_priced += 1
        by_event.setdefault(event_pid, []).append(
            EventMarket(
                market_id=r["market_polymarket_id"],
                yes_price=live_price,
                # side=NO → сохраняем NO-токен (index 1). Раньше клали yes_token_id:
                # в live бот купил бы YES-токен фаворита при намерении NO (ставка
                # против сигнала). В virtual баг был невидим — P&L считается по
                # entry_price=1−YES, а не по фактическому токену.
                token_id=no_token_id(r.get("clob_token_ids")),
            )
        )

    repo = get_detector_signal_repository()
    n_signals = 0
    n_inserted = 0
    n_skipped_open = 0
    for event_id, markets in by_event.items():
        if event_id in open_events:
            n_skipped_open += 1
            continue
        signals = d3_cross_mkt_arb.detect(
            event_id=event_id,
            markets=markets,
            neg_risk=True,
            neg_risk_other=False,
            n_yes_winners=1,
        )
        for signal in signals:
            n_signals += 1
            try:
                if await repo.insert(signal):
                    n_inserted += 1
                    open_events.add(event_id)
                    await _enqueue_for_execution(signal)
            except Exception as exc:
                logger.warning("d3_scan_insert_failed", error=str(exc), market=signal.market_id)

    logger.info(
        "d3_scan_completed",
        events=len(by_event),
        markets=len(rows),
        signals=n_signals,
        inserted=n_inserted,
        skipped_open=n_skipped_open,
        live_priced=f"{n_live_priced}/{len(rows)}",
    )
    return {
        "events": len(by_event),
        "markets": len(rows),
        "signals": n_signals,
        "inserted": n_inserted,
        "skipped_open": n_skipped_open,
        "live_priced": n_live_priced,
    }
