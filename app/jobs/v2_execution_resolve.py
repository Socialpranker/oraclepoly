"""V2 Execution Resolve — cron */1 min.

Делает три вещи:
  1. Маркет-ордера (taker FOK) — после place мы записали 'placed', но FOK
     либо filled моментально, либо отказан. Подтягиваем status из CLOB.
  2. Лимит-ордера (maker GTC) — стейл-кансел через 5 минут если не filled.
  3. Settlement: если detector_signals.resolved=true и order.status='filled' →
     запись pnl (gross) + net_pnl (после fee) + status='settled'.

PnL хранится в двух формах:
  - `pnl` (gross): для backwards-compat с compat VIEW и legacy reports.
  - `net_pnl`: реалистичный PnL после Polymarket fee. Slippage НЕ вычитается
    отдельно — fill_price уже отражает цену исполнения со slippage (fix
    2026-07-16, был двойной счёт). Дашборд показывает net через COALESCE(net_pnl, pnl).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import structlog

from app.analytics.notification import Notification, Severity
from app.core.config import get_settings
from app.jobs.detector_signals_resolve import _signal_outcome
from app.services.database import execute, fetch
from app.services.fee_calculator import estimate_trading_fee
from app.services.jsonb_utils import parse_jsonb
from app.services.notification_bus import publish

logger = structlog.get_logger(__name__)

STALE_MAKER_TIMEOUT_SEC: int = 300  # 5 минут — после этого maker отменяется
FOK_STALE_TIMEOUT_SEC: int = 60  # FOK мгновенен — вис >60с в 'placed' = баг, реанимируем


async def _build_clob_client():
    from py_clob_client_v2 import ApiCreds

    from app.services.polymarket_service import PolymarketService

    settings = get_settings()
    svc = PolymarketService()
    client = svc.clob_client
    creds = ApiCreds(
        api_key=settings.polymarket_api_key,
        api_secret=settings.polymarket_api_secret,
        api_passphrase=settings.polymarket_api_passphrase,
    )
    client.set_api_creds(creds)
    return client


async def _poll_placed_orders(client) -> dict[str, int]:
    """Подтянуть Polymarket status для всех placed orders."""
    rows = await fetch(
        """
        SELECT id, order_id, signal_price, limit_price, stake, order_type, placed_at
        FROM execution_orders
        WHERE status = 'placed' AND order_id IS NOT NULL
        """
    )
    counts = {"filled": 0, "cancelled": 0, "stale_cancelled": 0, "still_open": 0, "errors": 0}

    now = datetime.now(UTC)
    for o in rows:
        order_id = o["order_id"]
        try:
            resp = client.get_order(order_id)
        except Exception as e:
            counts["errors"] += 1
            logger.warning("poll_get_order_error", order_id=order_id, error=str(e)[:200])
            continue
        if not resp:
            counts["errors"] += 1
            continue

        pm_status = (resp.get("status") or "").upper()
        size_matched = float(resp.get("size_matched") or 0)
        original_size = float(resp.get("original_size") or o["stake"])

        # Полный fill
        if pm_status == "MATCHED" or (size_matched > 0 and size_matched >= original_size * 0.95):
            fill_price = float(resp.get("price", o.get("limit_price") or o["signal_price"]))
            fee_paid = float(resp.get("fee") or 0)
            await execute(
                """
                UPDATE execution_orders
                   SET status='filled', fill_price=$1, filled_qty=$2, fee_paid=$3,
                       filled_at=now()
                 WHERE id=$4
                """,
                fill_price,
                size_matched,
                fee_paid,
                o["id"],
            )
            counts["filled"] += 1
            continue

        # Polymarket-инициированная отмена
        if pm_status in ("CANCELLED", "EXPIRED"):
            if size_matched > 0:
                fill_price = float(resp.get("price", o.get("limit_price") or o["signal_price"]))
                fee_paid = float(resp.get("fee") or 0)
                await execute(
                    """
                    UPDATE execution_orders
                       SET status='filled', fill_price=$1, filled_qty=$2, fee_paid=$3,
                           stake=$4, filled_at=now()
                     WHERE id=$5
                    """,
                    fill_price,
                    size_matched,
                    fee_paid,
                    round(size_matched * fill_price, 2),
                    o["id"],
                )
                counts["filled"] += 1
            else:
                await execute(
                    """
                    UPDATE execution_orders
                       SET status='cancelled', cancel_reason=$1, cancelled_at=now()
                     WHERE id=$2
                    """,
                    f"polymarket_{pm_status.lower()}",
                    o["id"],
                )
                counts["cancelled"] += 1
            continue

        # Stale-cancel для maker (GTC) после timeout
        if o["order_type"] == "GTC" and o.get("placed_at"):
            placed_at = o["placed_at"]
            if placed_at.tzinfo is None:
                placed_at = placed_at.replace(tzinfo=UTC)
            age = (now - placed_at).total_seconds()
            if age > STALE_MAKER_TIMEOUT_SEC:
                try:
                    client.cancel(order_id)
                except Exception as e:
                    logger.warning("stale_cancel_error", order_id=order_id, error=str(e)[:200])
                if size_matched > 0:
                    fill_price = float(resp.get("price", o.get("limit_price") or o["signal_price"]))
                    fee_paid = float(resp.get("fee") or 0)
                    await execute(
                        """
                        UPDATE execution_orders
                           SET status='filled', fill_price=$1, filled_qty=$2, fee_paid=$3,
                               stake=$4, filled_at=now()
                         WHERE id=$5
                        """,
                        fill_price,
                        size_matched,
                        fee_paid,
                        round(size_matched * fill_price, 2),
                        o["id"],
                    )
                    counts["filled"] += 1
                else:
                    await execute(
                        """
                        UPDATE execution_orders
                           SET status='cancelled', cancel_reason='stale_timeout',
                               cancelled_at=now()
                         WHERE id=$1
                        """,
                        o["id"],
                    )
                    counts["stale_cancelled"] += 1
                continue

        # FOK (taker) не должен висеть в 'placed' — исполняется мгновенно или
        # отклоняется. Вис >FOK_STALE_TIMEOUT_SEC = баг записи статуса или CLOB.
        # (fix 2026-07-16, аудит: 1 такой ордер держал inflight-слот 4+ дня)
        if o["order_type"] != "GTC" and o.get("placed_at"):
            placed_at = o["placed_at"]
            if placed_at.tzinfo is None:
                placed_at = placed_at.replace(tzinfo=UTC)
            age = (now - placed_at).total_seconds()
            if age > FOK_STALE_TIMEOUT_SEC:
                size_matched = float(resp.get("size_matched") or 0)
                if size_matched <= 0:
                    await execute(
                        """
                        UPDATE execution_orders
                           SET status='rejected', rejection_reason='fok_stale_unfilled',
                               is_transient_reject=true, cancelled_at=now()
                         WHERE id=$1
                        """,
                        o["id"],
                    )
                    counts["fok_rejected"] = counts.get("fok_rejected", 0) + 1
                    continue
                # size_matched>0 но статус неопределён — не трогаем, эскалируем
                from app.core.task_queue import debounce_acquire

                if await debounce_acquire(f"stale_fok_alert:{o['id']}", ttl=3600):
                    try:
                        await publish(
                            [
                                Notification(
                                    severity=Severity.ACTION_REQUIRED,
                                    subject=f"order:{o['id']}",
                                    kind="fok_order_stale",
                                    headline="Ордер завис в неопределённом состоянии",
                                    what_happened=[
                                        f"FOK-ордер больше {FOK_STALE_TIMEOUT_SEC}с числится размещённым, "
                                        f"хотя часть его уже исполнилась (size_matched={size_matched}).",
                                        "Автоматика такой ордер не трогает: непонятно, исполнен он или нет.",
                                    ],
                                    what_if_ignored=(
                                        "Позиция может быть открыта на бирже, но не учтена в базе — "
                                        "сверка засчитает это как расхождение."
                                    ),
                                    what_to_do=f"Проверь ордер {order_id} на CLOB и приведи статус в базе в соответствие.",
                                    actions=[],
                                    evidence=[f"order_id={order_id}", f"size_matched={size_matched}"],
                                )
                            ],
                            actor="v2_execution_resolve",
                        )
                        counts["fok_alerted"] = counts.get("fok_alerted", 0) + 1
                    except Exception as notify_exc:
                        logger.warning("stale_fok_notify_failed", error=str(notify_exc)[:200])
                counts["still_open"] += 1
                continue

        counts["still_open"] += 1

    return counts


_POLL_EXITING_SQL = """
SELECT eo.id, eo.exit_order_id, eo.side, eo.fill_price, eo.stake, eo.exit_reason,
       COALESCE(m.category, 'other') AS category,
       m.fee_schedule
  FROM execution_orders eo
  LEFT JOIN markets m ON m.polymarket_id = eo.market_id
 WHERE eo.status = 'exiting' AND eo.exit_order_id IS NOT NULL
"""


async def _poll_exiting_orders(client) -> dict[str, int]:
    """Live exit path (audit топ-1, 2026-07-02): подтянуть статус SELL-ордеров,
    выставленных close_live_position (TG-кнопка/endpoint/position_manager TP-SL-max_hold).

    MATCHED (полный/почти-полный fill, тот же порог 95%, что и _poll_placed_orders) →
    settled, outcome=EARLY_EXIT, realized PnL от фактической exit-цены
    (_compute_early_exit_pnl, тот же расчёт, что и virtual TP/SL — round-trip fee+slippage).

    CANCELLED/EXPIRED (FOK не исполнился, size_matched=0) → назад в 'filled',
    exit_order_id/exit_requested_at очищаются — следующий тик position_manager
    (или человек через TG-кнопку) попробует снова. Алерт админу — позиция всё ещё
    открыта, exit не прошёл.
    """
    from app.jobs.position_manager import _compute_early_exit_pnl

    rows = await fetch(_POLL_EXITING_SQL)
    counts = {"settled": 0, "reopened": 0, "still_open": 0, "errors": 0}
    if not rows:
        return counts

    for o in rows:
        exit_order_id = o["exit_order_id"]
        try:
            resp = client.get_order(exit_order_id)
        except Exception as e:
            counts["errors"] += 1
            logger.warning("poll_exiting_get_order_error", order_id=exit_order_id, error=str(e)[:200])
            continue
        if not resp:
            counts["errors"] += 1
            continue

        pm_status = (resp.get("status") or "").upper()
        size_matched = float(resp.get("size_matched") or 0)
        original_size = float(resp.get("original_size") or 0)

        if pm_status == "MATCHED" or (size_matched > 0 and size_matched >= original_size * 0.95):
            exit_price = float(resp.get("price") or 0.0)
            pnl = _compute_early_exit_pnl(
                side=o["side"],
                stake=float(o["stake"]),
                fill_price=float(o["fill_price"] or 0.0),
                exit_price=exit_price,
                category=o["category"],
                fee_schedule=parse_jsonb(o["fee_schedule"]),
            )
            await execute(
                """
                UPDATE execution_orders
                   SET status='settled', outcome='EARLY_EXIT', exit_price=$2, exit_at=now(),
                       pnl=$3, trading_fee_usd=$4, slippage_usd=$5, net_pnl=$6, settled_at=now()
                 WHERE id=$1 AND status='exiting'
                """,
                o["id"],
                round(exit_price, 4),
                pnl["pnl"],
                pnl["trading_fee_usd"],
                pnl["slippage_usd"],
                pnl["net_pnl"],
            )
            counts["settled"] += 1
            continue

        if pm_status in ("CANCELLED", "EXPIRED"):
            await execute(
                """
                UPDATE execution_orders
                   SET status='filled', exit_order_id=NULL, exit_requested_at=NULL
                 WHERE id=$1 AND status='exiting'
                """,
                o["id"],
            )
            counts["reopened"] += 1
            try:
                await publish(
                    [
                        Notification(
                            severity=Severity.FYI,
                            subject=f"order:{o['id']}",
                            kind="exit_reopened",
                            headline="Выход из позиции не состоялся, пробуем снова",
                            what_happened=[
                                f"Ордер на закрытие {pm_status.lower()}, причина выхода — {o['exit_reason']}.",
                                "Позиция снова считается открытой, следующий тик попробует закрыть её заново.",
                            ],
                            what_if_ignored=None,
                            what_to_do="Ничего делать не нужно — повтор произойдёт автоматически.",
                            actions=[],
                            evidence=[f"order_id={o['id']}", f"pm_status={pm_status}"],
                        )
                    ],
                    actor="v2_execution_resolve",
                )
            except Exception as notify_exc:
                logger.warning("exit_reopen_notify_failed", error=str(notify_exc)[:200])
            continue

        counts["still_open"] += 1

    return counts


_STUCK_EXITING_SQL = """
SELECT id, exit_order_id, exit_reason, exit_requested_at
  FROM execution_orders
 WHERE status = 'exiting' AND exit_requested_at < now() - interval '15 minutes'
"""


async def _reanimate_stuck_exiting(client) -> dict[str, int]:
    """Task (live exit hardening, продолжение audit топ-1): 'exiting' виснет без
    предупреждения, если CLOB отвечает "всё ещё открыт" (или недоступен) дольше
    обычного цикла _poll_exiting_orders. В отличие от _reanimate_stuck_intents —
    здесь предполагается, что реальный SELL уже размещён на бирже, поэтому статус
    НЕ трогаем автоматически (риск начать второй SELL поверх живого первого).
    Только эскалируем человеку через send_admin_message, debounced на час на
    ордер, чтобы не спамить каждую минуту одним и тем же зависшим ордером.

    Если get_order показывает, что статус уже реально изменился (MATCHED/
    CANCELLED/EXPIRED), это не наш случай — обычный _poll_exiting_orders,
    вызванный раньше в этом же тике run_v2_execution_resolve, уже забрал
    settle/reopen. Пропускаем без алерта.
    """
    from app.core.task_queue import debounce_acquire

    rows = await fetch(_STUCK_EXITING_SQL)
    counts = {"alerted": 0}
    if not rows:
        return counts

    for row in rows:
        exit_order_id = row["exit_order_id"]
        still_stuck = True
        try:
            resp = client.get_order(exit_order_id)
            if resp:
                pm_status = (resp.get("status") or "").upper()
                size_matched = float(resp.get("size_matched") or 0)
                original_size = float(resp.get("original_size") or 0)
                resolved = pm_status == "MATCHED" or (size_matched > 0 and size_matched >= original_size * 0.95)
                resolved = resolved or pm_status in ("CANCELLED", "EXPIRED")
                still_stuck = not resolved
        except Exception as e:
            logger.warning("stuck_exiting_get_order_error", order_id=exit_order_id, error=str(e)[:200])
            still_stuck = True

        if not still_stuck:
            continue

        if not await debounce_acquire(f"stuck_exiting_alert:{row['id']}", ttl=3600):
            continue

        try:
            await publish(
                [
                    Notification(
                        severity=Severity.ACTION_REQUIRED,
                        subject=f"order:{row['id']}",
                        kind="exit_order_stuck",
                        headline="Ордер на закрытие завис дольше 15 минут",
                        what_happened=[
                            f"Выход из позиции ({row['exit_reason']}) не завершился и не отменился.",
                            "Автоматика статус не меняет: неизвестно, исполнился ордер или нет.",
                        ],
                        what_if_ignored=(
                            "Позиция висит между открытой и закрытой: ни выход не отработал, ни повтор не запустится."
                        ),
                        what_to_do=f"Проверь ордер {exit_order_id} на CLOB и приведи статус в базе в соответствие.",
                        actions=[],
                        evidence=[f"order_id={row['id']}", f"exit_order_id={exit_order_id}"],
                    )
                ],
                actor="v2_execution_resolve",
            )
            counts["alerted"] += 1
        except Exception as notify_exc:
            logger.warning("stuck_exiting_notify_failed", error=str(notify_exc)[:200])

    return counts


_STUCK_INTENT_SQL = """
SELECT eo.id, eo.market_id, eo.detector, eo.stake, eo.limit_price, eo.created_at, ds.token_id
  FROM execution_orders eo
  JOIN detector_signals ds ON ds.id = eo.detector_signal_id
 WHERE eo.status = 'intent' AND eo.created_at < now() - interval '10 minutes'
"""


async def _reanimate_stuck_intents(client) -> dict[str, int]:
    """Task 2 (audit топ-4): 'intent' виснет навечно, если процесс упал между
    insert и place (до записи 'placed'/'rejected'). Раз в минуту подтягиваем
    intent-строки старше 10 минут и сверяемся с CLOB: если ордер реально жив
    (POST дошёл, ответ не дошёл до нас) — доводим до 'placed'; если нет — закрываем
    как 'rejected' (транзиентно, ретраится) и уведомляем админа — нештатная ситуация.

    expected_size + not_before (audit: ужесточение матчинга) — иначе чужой
    resting-ордер на том же token_id ложно засчитается как наш intent.
    """
    from app.services.clob_order_lookup import find_our_open_order

    rows = await fetch(_STUCK_INTENT_SQL)
    counts = {"recovered": 0, "closed": 0}
    if not rows:
        return counts

    for row in rows:
        token_id = row["token_id"]
        limit_price = row.get("limit_price")
        stake = row.get("stake")
        expected_size = float(stake) / float(limit_price) if stake and limit_price else None
        recovered_id = (
            await find_our_open_order(
                client,
                token_id,
                expected_size=expected_size,
                not_before=row.get("created_at"),
            )
            if token_id
            else None
        )
        if recovered_id:
            await execute(
                "UPDATE execution_orders SET status='placed', order_id=$1, placed_at=now() WHERE id=$2",
                recovered_id,
                row["id"],
            )
            counts["recovered"] += 1
            outcome = f"восстановлен как placed (order_id={recovered_id})"
            recovered_ok = True
        else:
            await execute(
                """
                UPDATE execution_orders
                   SET status='rejected', rejection_reason='place_crash_recovered', is_transient_reject=true
                 WHERE id=$1
                """,
                row["id"],
            )
            counts["closed"] += 1
            outcome = "не найден на CLOB — закрыт как rejected (транзиентно, ретраится)"
            recovered_ok = False

        try:
            # Два РАЗНЫХ исхода — два разных kind. С общим kind сообщение
            # «ордер нашёлся и восстановлен» подавилось бы cooldown'ом
            # предыдущего «ордер потерян и закрыт», и наоборот (урок пачки B).
            await publish(
                [
                    Notification(
                        severity=Severity.FYI,
                        subject=f"order:{row['id']}",
                        kind="intent_recovered" if recovered_ok else "intent_closed_as_rejected",
                        headline=(
                            "Застрявший ордер найден и восстановлен"
                            if recovered_ok
                            else "Застрявший ордер закрыт: на бирже его нет"
                        ),
                        what_happened=[
                            f"Детектор {row['detector']}, рынок {row['market_id']}.",
                            outcome.capitalize() + ".",
                        ],
                        what_if_ignored=None,
                        what_to_do=(
                            "Ничего делать не нужно — ордер вернулся в нормальный цикл."
                            if recovered_ok
                            else "Ничего делать не нужно — сигнал уйдёт в повтор на следующем тике."
                        ),
                        actions=[],
                        evidence=[f"order_id={row['id']}", f"recovered={recovered_ok}"],
                    )
                ],
                actor="v2_execution_resolve",
            )
        except Exception as notify_exc:
            logger.warning("stuck_intent_notify_failed", error=str(notify_exc)[:200])

    return counts


_FETCH_SETTLEABLE_SQL = """
SELECT eo.id,
       eo.execution_mode,
       eo.fill_price,
       eo.stake,
       eo.side,
       eo.market_id,
       eo.as_penalty_usd,
       ds.outcome,
       m.winner,
       COALESCE(m.category, 'other') AS category,
       m.fee_schedule,
       m.question
  FROM execution_orders eo
  JOIN detector_signals ds ON ds.id = eo.detector_signal_id
  LEFT JOIN markets m       ON m.polymarket_id = eo.market_id
 WHERE eo.status IN ('filled', 'virtual_filled')
   -- Audit P0 (research/fable-gates-audit-2026-07-03 §3.1): live-ордер settle
   -- только по резолву РЫНКА. EXPIRED сигнала (таймаут horizon_sec×N) не
   -- трогает живую позицию — шэры остаются на бирже до markets.winner.
   -- ds.resolved гейтит только virtual: live-outcome выводится из m.winner,
   -- и ожидание часового тика detector-signals-resolve (:40) добавляло до
   -- часа лага между markets.winner и settle (инцидент 724f3e71, 2026-07-07).
   AND (
        (eo.execution_mode = 'virtual' AND ds.resolved = true)
        OR (eo.execution_mode != 'virtual' AND m.resolved = true AND m.winner IN ('yes', 'no'))
   )
"""

_OUTCOME_EMOJI = {"WIN": "🟢", "LOSS": "🔴", "EXPIRED": "⏱"}


def settlement_notification(row: dict, net_pnl: float) -> Notification:
    """Итог сделки — деньги уже посчитаны и записаны.

    FYI: сделка закрыта, решать владельцу нечего. Уведомление — отчёт о факте,
    а не запрос действия, поэтому ACTION_REQUIRED здесь был бы обесцениванием
    уровня (§4).
    """
    outcome = row["outcome"]
    emoji = _OUTCOME_EMOJI.get(outcome, "")
    question = row.get("question") or row.get("market_id") or "?"
    stake = float(row["stake"])
    side = row.get("side") or ""

    if outcome == "EXPIRED":
        lines = [
            f"{question} — {side}.",
            f"Ставка {stake:.2f}$ сгорела: истёк срок сигнала, а не резолв рынка.",
        ]
        headline = f"{emoji} Сигнал истёк, ставка сгорела"
    else:
        final = stake + net_pnl
        lines = [
            f"{question} — {side}.",
            f"Ставка {stake:.2f}$ превратилась в {final:.2f}$ — итог {net_pnl:+.2f}$ после комиссий.",
        ]
        headline = f"{emoji} Сделка закрыта: {net_pnl:+.2f}$"

    return Notification(
        severity=Severity.FYI,
        # Subject свой у каждой сделки: общий подавил бы вторую и третью
        # закрытые сделки cooldown'ом первой.
        subject=f"order:{row.get('id')}",
        kind="trade_settled",
        headline=headline,
        what_happened=lines,
        what_if_ignored=None,
        what_to_do="Ничего делать не нужно — сделка уже закрыта, деньги посчитаны.",
        actions=[],
        evidence=[f"outcome={outcome}", f"net_pnl={net_pnl:+.2f}", f"stake={stake:.2f}"],
    )


async def _notify_settlement(row: dict, net_pnl: float) -> None:
    """Live per-trade TG-уведомление о WIN/LOSS/EXPIRED (не блокирует settlement)."""
    try:
        await publish([settlement_notification(row, net_pnl)], actor="v2_execution_resolve")
    except Exception as e:
        logger.warning("settlement_notify_failed", order_id=row.get("id"), error=str(e)[:200])


_SETTLE_UPDATE_SQL = """
UPDATE execution_orders
   SET status            = data.status,
       outcome           = data.outcome,
       pnl               = data.pnl,
       trading_fee_usd   = data.trading_fee_usd,
       slippage_usd      = data.slippage_usd,
       net_pnl           = data.net_pnl,
       -- 2026-05-26 RCA: маркируем resolution exits явно (раньше exit_reason оставался
       -- NULL для resolved trades, что мешало dashboard breakdowns. См.
       -- research/detector-rca-2026-05-26/). exit_reason для early-exit (TP/SL/MZE)
       -- проставляется отдельно в position_manager._EARLY_EXIT_UPDATE_SQL.
       exit_reason       = COALESCE(execution_orders.exit_reason, 'resolution'),
       settled_at        = now()
  FROM (
    SELECT
      unnest($1::uuid[])      AS id,
      unnest($2::text[])      AS status,
      unnest($3::text[])      AS outcome,
      unnest($4::numeric[])   AS pnl,
      unnest($5::numeric[])   AS trading_fee_usd,
      unnest($6::numeric[])   AS slippage_usd,
      unnest($7::numeric[])   AS net_pnl
  ) AS data
 WHERE execution_orders.id = data.id
   -- Settle guard (audit second-tier): не перезаписывать строку, которую
   -- position_manager/live_exit параллельно перевёл в другой статус (напр.
   -- 'exiting' при аварийном close/TP/SL) — иначе гонка резолва vs exit-пути
   -- искажает PnL, который кормит promote-гейты. _FETCH_SETTLEABLE_SQL уже
   -- фильтрует по status IN ('filled', 'virtual_filled') на момент SELECT, но
   -- между SELECT и этим UPDATE статус мог измениться — повторная проверка
   -- в WHERE закрывает окно гонки.
   AND execution_orders.status IN ('filled', 'virtual_filled')
"""


def _compute_settlement_pnl(
    *,
    stake: float,
    fill_price: float,
    outcome: str,
    category: str,
    fee_schedule: dict | None = None,
    as_penalty_usd: float | None = None,
) -> dict[str, float]:
    """Посчитать gross + net PnL для одной settled позиции.

    Polymarket fee model (docs.polymarket.com/trading/fees):
      fee_per_trade = trade_amount_usd × feeRate × (p × (1-p))^exponent

      "trade_amount_usd" — это $ объём ИМЕННО торговли. Резолюция (winning shares
      выкупаются за $1.00 each) — НЕ торговля, fee = 0. См. fee_calculator.py.

    Settlement (WIN или LOSS) — это **резолюция**, не активная торговля на exit.
    Платим только 1 ногу (entry), fee = 1× fee(amount=stake, p=fill).

    Slippage vs adverse-selection penalty (важное различие live↔virtual):
    - LIVE (maker/taker): fill_price = реальный CLOB resp["price"] (v2_..._resolve:83),
      slippage УЖЕ встроен в цену исполнения. Отдельный stake×half_spread был двойным
      счётом — убран 2026-07-16 (аудит воронки D3). Вызывающий код передаёт
      as_penalty_usd=None → penalty=0, поведение неизменно.
    - VIRTUAL: fill_price = сырой book-ask БЕЗ penalty; adverse-selection стоимость
      (P_informed × half_spread × shares, уже в $) оседает в execution_orders.as_penalty_usd
      и раньше НЕ вычиталась из net_pnl (slippage_usd хардкодился 0.0). Баг 2026-07-21:
      завышал ВСЕ virtual net_pnl (D38-temperature +30%: $362 из $1204). Теперь penalty
      вычитается из net и пишется в slippage_usd (видимость в БД/отчётах).
      WIN:  gross = stake/fill − stake ; net = gross − fee − as_penalty
      LOSS: gross = −stake            ; net = gross − fee − as_penalty

    Inflated-fee bug fix 2026-05-18: раньше WIN использовало 2×fee и
    gross_notional=stake/fill ($250 вместо $100 для stake=$100, fill=0.40) для
    amount-нога fee'й. Реально trade_amount на entry = stake (=$100), exit = 0.

    SSoT — этот файл + app.services.pnl_formulas.compute_net_pnl (mirror).
    Early-exit (TP/SL/MZE) использует другую формулу в position_manager.py
    (там есть exit нога с активной торговлей).
    """
    cat = (category or "other").lower()
    eff = max(fill_price, 0.001)

    if outcome == "WIN":
        gross_pnl = stake / eff - stake  # = stake × (1/eff − 1)
        fee_entry = estimate_trading_fee(stake, eff, cat, fee_schedule)
        fee_total = fee_entry
    elif outcome == "LOSS":
        gross_pnl = -stake
        fee_entry = estimate_trading_fee(stake, eff, cat, fee_schedule)
        fee_total = fee_entry
    else:
        # Edge case: outcome не определен (нечего settle), вернём nuls.
        return {
            "pnl": 0.0,
            "trading_fee_usd": 0.0,
            "slippage_usd": 0.0,
            "net_pnl": 0.0,
        }

    # adverse-selection penalty: 0 для live (fill_price уже включает slippage),
    # реальная $-стоимость для virtual (см. docstring). Кладём в slippage_usd для
    # видимости и вычитаем из net_pnl.
    penalty = float(as_penalty_usd or 0.0)
    net_pnl = gross_pnl - fee_total - penalty

    return {
        "pnl": round(gross_pnl, 4),
        "trading_fee_usd": round(fee_total, 4),
        "slippage_usd": round(penalty, 4),
        "net_pnl": round(net_pnl, 4),
    }


async def _settle_filled() -> int:
    """Settle filled orders: gross + net PnL via fetch+compute+bulk update.

    Заменяет одну SQL-only update'у. Причина — нужен JOIN с markets для category
    и формула fee (huber на share count) слишком сложная для inline SQL.
    """
    rows = await fetch(_FETCH_SETTLEABLE_SQL)
    if not rows:
        return 0

    ids: list[Any] = []
    statuses: list[str] = []
    outcomes: list[str] = []
    pnls: list[float] = []
    fees: list[float] = []
    slippages: list[float] = []
    net_pnls: list[float] = []
    to_notify: list[tuple[dict, float]] = []

    for row in rows:
        is_virtual = row["execution_mode"] == "virtual"
        if is_virtual:
            outcome = row["outcome"] or ""
            if outcome not in ("WIN", "LOSS", "EXPIRED"):
                # ds.resolved=true но outcome пустой — пропускаем (не должно происходить)
                continue
        else:
            # Live: истина — рынок, не сигнал. ds.outcome может быть EXPIRED
            # при уже известном winner (ретро-резолв не гарантирован).
            outcome = _signal_outcome(row["side"], row["winner"])
            if outcome is None:
                logger.warning("settle_live_no_winner", order_id=str(row["id"]), market_id=row["market_id"])
                continue
        pnl = _compute_settlement_pnl(
            stake=float(row["stake"]),
            fill_price=float(row["fill_price"] or 0.0),
            outcome=outcome,
            category=row["category"],
            fee_schedule=parse_jsonb(row["fee_schedule"]),
            # adverse-selection penalty вычитаем ТОЛЬКО для virtual: у live fill_price
            # уже включает реальный slippage (двойной счёт запрещён). Баг 2026-07-21.
            as_penalty_usd=(row.get("as_penalty_usd") if is_virtual else None),
        )
        new_status = "virtual_settled" if is_virtual else "settled"
        ids.append(row["id"])
        statuses.append(new_status)
        outcomes.append(outcome)
        pnls.append(pnl["pnl"])
        fees.append(pnl["trading_fee_usd"])
        slippages.append(pnl["slippage_usd"])
        net_pnls.append(pnl["net_pnl"])

        if not is_virtual:
            to_notify.append((row, pnl["net_pnl"]))

    if not ids:
        return 0

    await execute(
        _SETTLE_UPDATE_SQL,
        ids,
        statuses,
        outcomes,
        pnls,
        fees,
        slippages,
        net_pnls,
    )

    # Уведомляем ТОЛЬКО после commit'а UPDATE (не до) — иначе крэш процесса между
    # notify и execute() оставляет строку в старом status, и следующий cron-тик
    # пришлёт дублирующее уведомление за ту же сделку (см. code-review 2026-07-05).
    # Параллельно (gather), а не последовательно — иначе TG-latency (timeout=10s
    # per admin, no connection pooling) масштабируется линейно с числом settled строк.
    if to_notify:
        await asyncio.gather(*(_notify_settlement(row, net_pnl) for row, net_pnl in to_notify))

    return len(ids)


_VIRTUAL_PLACED_SQL = """
SELECT eo.id, eo.market_id, eo.side, eo.limit_price, eo.stake, eo.placed_at, eo.signal_price,
       COALESCE(
           ds.token_id,
           CASE
               WHEN eo.side = 'YES' THEN m.clob_token_ids->>0
               WHEN eo.side = 'NO'  THEN m.clob_token_ids->>1
           END
       ) AS token_id
  FROM execution_orders eo
  JOIN detector_signals ds ON ds.id = eo.detector_signal_id
  LEFT JOIN markets m ON m.polymarket_id = eo.market_id
 WHERE eo.status = 'virtual_placed' AND eo.placed_at IS NOT NULL
"""


async def _poll_virtual_placed() -> dict[str, int]:
    """Для virtual_placed maker — fetch CLOB book, проверить fill vs stale."""
    from app.services.virtual_execution import simulate_resolve_maker

    rows = await fetch(_VIRTUAL_PLACED_SQL)
    counts = {"virtual_filled": 0, "virtual_cancelled": 0, "still_open": 0, "errors": 0}

    now = datetime.now(UTC)
    for o in rows:
        token_id = o["token_id"]
        if not token_id:
            counts["errors"] += 1
            continue
        placed_at = o["placed_at"]
        if placed_at.tzinfo is None:
            placed_at = placed_at.replace(tzinfo=UTC)
        age_sec = (now - placed_at).total_seconds()
        try:
            res = await simulate_resolve_maker(
                token_id=token_id,
                limit_price=float(o["limit_price"]),
                age_sec=age_sec,
            )
        except Exception as e:
            counts["errors"] += 1
            logger.warning("virtual_resolve_error", order_id=str(o["id"]), error=str(e)[:200])
            continue

        if res.status == "virtual_filled":
            await execute(
                """
                UPDATE execution_orders
                   SET status='virtual_filled', fill_price=$1, filled_qty=stake/$1, filled_at=now()
                 WHERE id=$2
                """,
                float(res.fill_price),
                o["id"],
            )
            counts["virtual_filled"] += 1
        elif res.status == "virtual_cancelled":
            await execute(
                """
                UPDATE execution_orders
                   SET status='virtual_cancelled', cancel_reason=$1, cancelled_at=now()
                 WHERE id=$2
                """,
                res.reason,
                o["id"],
            )
            counts["virtual_cancelled"] += 1
        else:
            counts["still_open"] += 1
    return counts


async def run_v2_execution_resolve() -> dict[str, Any]:
    """Минутный тик resolve-джобы.

    Порядок шагов (audit 0.2, 2026-07-10): _settle_filled идёт ПЕРВЫМ, до
    poll/exiting/reanimate/virtual-хвоста. _settle_filled — live-путь (реальные
    деньги, вход для ramp_check и redeem) и читает только БД (markets.resolved/
    winner пишут другие джобы, книги ему не нужны), поэтому ему не нужно ждать
    остальные шаги тика — а virtual-хвост (_poll_virtual_placed) может занимать
    десятки секунд и раньше держал live-settle в конце очереди. Остальной
    порядок (poll placed → stuck_exiting → exiting → stuck_intent → virtual)
    не менялся — это Polymarket API-опросы, для них последовательность неважна.
    """
    counts: dict[str, Any] = {
        "poll": {},
        "virtual": {},
        "settled": 0,
        "reanimated": {},
        "exiting": {},
        "stuck_exiting": {},
    }

    # Settle filled (real + virtual) при resolved сигнале — первым, чтобы live-settle
    # не ждал virtual-хвост ниже. Читает только БД, книги/CLOB не нужны.
    # Ошибка settle не должна блокировать poll филлов ниже (раньше settle шёл
    # последним и его падение не мешало остальным шагам тика).
    try:
        counts["settled"] = await _settle_filled()
    except Exception as e:
        logger.error("v2_resolve_settle_error", error=str(e)[:200])

    # Poll Polymarket для real placed
    has_placed = await fetch("SELECT 1 FROM execution_orders WHERE status='placed' AND order_id IS NOT NULL LIMIT 1")
    if has_placed:
        try:
            client = await _build_clob_client()
            counts["poll"] = await _poll_placed_orders(client)
        except Exception as e:
            logger.error("v2_resolve_poll_error", error=str(e)[:200])
            counts["poll"] = {"errors": 1}

    # Reanimate: exiting-ордера старше 15 минут — алерт, без авто-отката (live exit hardening).
    has_stuck_exiting = await fetch(
        "SELECT 1 FROM execution_orders WHERE status = 'exiting' AND exit_requested_at < now() - interval '15 minutes' LIMIT 1"
    )
    if has_stuck_exiting:
        try:
            client = await _build_clob_client()
            counts["stuck_exiting"] = await _reanimate_stuck_exiting(client)
        except Exception as e:
            logger.error("v2_resolve_stuck_exiting_error", error=str(e)[:200])
            counts["stuck_exiting"] = {"errors": 1}

    # Live exit path (audit топ-1, 2026-07-02): подтянуть статус SELL-ордеров close_live_position.
    has_exiting = await fetch(
        "SELECT 1 FROM execution_orders WHERE status='exiting' AND exit_order_id IS NOT NULL LIMIT 1"
    )
    if has_exiting:
        try:
            client = await _build_clob_client()
            counts["exiting"] = await _poll_exiting_orders(client)
        except Exception as e:
            logger.error("v2_resolve_exiting_poll_error", error=str(e)[:200])
            counts["exiting"] = {"errors": 1}

    # Task 2 (audit топ-4): реанимация intent-строк, застрявших дольше 10 минут
    # (процесс упал между insert и place).
    has_stuck_intent = await fetch(
        "SELECT 1 FROM execution_orders WHERE status = 'intent' AND created_at < now() - interval '10 minutes' LIMIT 1"
    )
    if has_stuck_intent:
        try:
            client = await _build_clob_client()
            counts["reanimated"] = await _reanimate_stuck_intents(client)
        except Exception as e:
            logger.error("v2_resolve_reanimate_error", error=str(e)[:200])
            counts["reanimated"] = {"errors": 1}

    # Poll virtual_placed (maker GTC simulation)
    has_virtual = await fetch("SELECT 1 FROM execution_orders WHERE status='virtual_placed' LIMIT 1")
    if has_virtual:
        try:
            counts["virtual"] = await _poll_virtual_placed()
        except Exception as e:
            logger.error("v2_resolve_virtual_error", error=str(e)[:200])
            counts["virtual"] = {"errors": 1}

    logger.info("v2_execution_resolve_done", **counts)
    return counts
