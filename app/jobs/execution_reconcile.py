"""Reconciliation job — сверка трейдов Polymarket с execution_orders.

Находит ордера на Polymarket без записи в БД (orphans) и синхронизирует bankroll.
Phase 1 Stage 4: 3-strike drift detection. При 3 подряд minutes с drift
(orphans>0 или wallet API недоступен) → SET bot:kill_switch.

Запускается cron каждую минуту.

См. docs/superpowers/plans/2026-05-risk-engine-hardening/04-reconciliation-60s.md.
"""

from __future__ import annotations

import structlog

from app.analytics.notification import Notification, Severity
from app.core.cache import get_redis
from app.core.config import get_settings
from app.services.database import execute, fetch, fetchval
from app.services.notification_bus import publish

logger = structlog.get_logger(__name__)

# Phase 1 Stage 4: drift strike pattern. Constants теперь читаются из Settings
# (Step 4.4 — env-overridable). Legacy module-level переменные сохранены для
# backward compat с tests, но runtime использует settings.reconciliation_*.
STRIKE_KEY: str = "bot:reconciliation_strikes"
KILL_SWITCH_KEY: str = "bot:kill_switch"
RECON_CLEAN_STREAK_KEY: str = "bot:reconciliation_clean_streak"
# Defaults для tests, не для production runtime — production читает Settings.
RECONCILIATION_STRIKE_THRESHOLD: int = 3
RECONCILIATION_STRIKE_TTL_SEC: int = 300


def _drift_detected(
    *,
    orphans: int,
    wallet_error: str | None,
    trades_error: str | None = None,
) -> tuple[bool, str]:
    """Classify reconciliation result как drift или clean.

    Three signals:
      - orphans > 0: trades на Polymarket без db записи.
      - wallet_error: cannot fetch balance → cannot verify state.
      - trades_error: cannot fetch trades feed → cannot detect orphans.
    Conservative: any signal → treat as drift.
    """
    if orphans > 0:
        return True, f"orphans={orphans}"
    if trades_error:
        return True, f"trades_error={trades_error[:50]}"
    if wallet_error:
        return True, f"wallet_error={wallet_error[:50]}"
    return False, ""


async def _handle_drift_strike(*, reason: str, stats: dict) -> None:
    """Increment strike counter. На threshold strikes → SET kill_switch.

    Args:
        reason: short label почему drift (orphans=N, wallet_error=...).
        stats: mutable dict для recording strike_count и kill_switch_set.
    """
    redis = await get_redis()
    if redis is None:
        logger.error("reconciliation_strike_redis_down", reason=reason)
        return

    settings = get_settings()
    threshold = settings.reconciliation_strike_threshold
    ttl_sec = settings.reconciliation_strike_ttl_sec

    strike = await redis.incr(STRIKE_KEY)
    await redis.expire(STRIKE_KEY, ttl_sec)
    stats["strike_count"] = strike

    # Drift вернулся → обнулить чистый streak (иначе авто-сброс мог бы сработать
    # на чередовании drift/clean).
    from app.services.kill_switch_clear import reset_clean_streak

    await reset_clean_streak(redis, RECON_CLEAN_STREAK_KEY)

    logger.warning("reconciliation_drift_detected", reason=reason, strike=strike, threshold=threshold)

    if strike >= threshold:
        await redis.set(
            KILL_SWITCH_KEY,
            f"reconciliation_drift:{reason}:strikes={strike}",
        )
        stats["kill_switch_set"] = True
        await _notify_kill_switch_triggered(reason=reason, strike=strike)
        logger.error("reconciliation_kill_switch_set", reason=reason, strike=strike)
    else:
        await _notify_drift_warning(reason=reason, strike=strike, threshold=threshold)


async def _reset_drift_strikes() -> None:
    """Clean state: drift gone → reset strike counter."""
    redis = await get_redis()
    if redis is None:
        return
    await redis.delete(STRIKE_KEY)


async def _maybe_auto_clear_kill_switch() -> None:
    """Авто-снятие транзиентного `reconciliation_drift:` kill_switch после N чистых циклов.

    Вызывается только в чистой ветке (drift не обнаружен). orphans>0 — реальный
    фолт — не даёт чистого цикла, streak не накопится. Persistent/чужие флаги не
    снимаются: см. kill_switch_clear.AUTO_CLEARABLE_PREFIXES.
    """
    settings = get_settings()
    if not settings.kill_switch_auto_clear_enabled:
        return
    redis = await get_redis()
    if redis is None:
        return
    from app.services.kill_switch_clear import try_auto_clear

    threshold = settings.kill_switch_auto_clear_clean_cycles
    cleared = await try_auto_clear(
        redis,
        streak_key=RECON_CLEAN_STREAK_KEY,
        threshold=threshold,
        owned_prefixes=("reconciliation_drift:",),
    )
    if cleared:
        await _notify_kill_switch_auto_cleared(clean_cycles=threshold)


# Три состояния одного инцидента — и у каждого СВОЙ kind. Общий kind означал бы,
# что cooldown шины подавляет «починилось» после «сломалось», и владелец остаётся
# с ложной картиной (урок пачки B, PR #739/#750).
_KIND_DRIFT_WARN = "reconciliation_drift_warn"
_KIND_KILL_SWITCH = "reconciliation_kill_switch"
_KIND_KILL_CLEARED = "reconciliation_kill_cleared"

_SUBJECT = "execution:reconciliation"


def drift_warning_notification(*, reason: str, strike: int, threshold: int) -> Notification:
    """Накопление drift-страйков, kill switch ещё не сработал."""
    return Notification(
        severity=Severity.ACTION_REQUIRED,
        subject=_SUBJECT,
        kind=_KIND_DRIFT_WARN,
        headline=f"Расхождение сверки, страйк {strike} из {threshold}",
        what_happened=[
            f"Сверка позиций разошлась: {reason}.",
            f"Это {strike}-й страйк подряд из {threshold}, после которых торговля встанет.",
        ],
        what_if_ignored=(
            f"Ещё {threshold - strike} таких прогона — и kill switch остановит live-торговлю автоматически."
        ),
        what_to_do=(
            "Проверь, совпадают ли позиции на Polymarket с execution_orders: "
            "чаще всего это сделка, прошедшая на бирже без записи в БД."
        ),
        actions=[],
        evidence=[f"strike={strike}/{threshold}", f"reason={reason}"],
    )


def kill_switch_notification(*, reason: str, strike: int) -> Notification:
    """Kill switch сработал — live-торговля остановлена."""
    return Notification(
        severity=Severity.ACTION_REQUIRED,
        subject=_SUBJECT,
        kind=_KIND_KILL_SWITCH,
        headline="Kill switch: live-торговля остановлена",
        what_happened=[
            f"{strike} расхождений сверки подряд: {reason}.",
            "Размещение live-ордеров заблокировано до ручного снятия.",
        ],
        what_if_ignored="Live-торговля стоит и сама не возобновится, пока замок не снят.",
        what_to_do=(
            "Разберись с причиной расхождения, потом сними замок: "
            "<code>redis-cli DEL bot:kill_switch bot:reconciliation_strikes</code>. "
            "Обычные причины — сделка на Polymarket без записи в БД (таймаут сети), "
            "недоступный wallet API, отозванный ключ или чужая сделка с того же кошелька."
        ),
        actions=[],
        evidence=[f"strikes={strike}", f"reason={reason}"],
    )


def kill_switch_cleared_notification(*, clean_cycles: int) -> Notification:
    """Recovery: замок снят автоматически.

    FYI, а не ACTION_REQUIRED: делать владельцу ничего не нужно, всё уже
    починилось само. Но и молчать нельзя — иначе он остаётся с картиной
    «торговля стоит», хотя она давно идёт.
    """
    return Notification(
        severity=Severity.FYI,
        subject=_SUBJECT,
        kind=_KIND_KILL_CLEARED,
        headline="Kill switch снят автоматически, торговля возобновлена",
        what_happened=[
            f"Расхождение сверки оказалось временным: {clean_cycles} чистых прогонов подряд.",
            "Замок снят, live-торговля идёт как обычно.",
        ],
        what_if_ignored=None,
        what_to_do="Ничего делать не нужно — сообщение чтобы ты знал, что торговля больше не заблокирована.",
        actions=[],
        evidence=[f"clean_cycles={clean_cycles}"],
    )


async def _notify_drift_warning(*, reason: str, strike: int, threshold: int) -> None:
    """TG warn на strike < threshold."""
    try:
        await publish(
            [drift_warning_notification(reason=reason, strike=strike, threshold=threshold)],
            actor="execution_reconcile",
        )
    except Exception as e:
        logger.warning("reconciliation_notify_warn_failed", error=str(e))


async def _notify_kill_switch_triggered(*, reason: str, strike: int) -> None:
    """TG kill alert."""
    try:
        await publish([kill_switch_notification(reason=reason, strike=strike)], actor="execution_reconcile")
    except Exception as e:
        logger.warning("reconciliation_notify_kill_failed", error=str(e))


async def _notify_kill_switch_auto_cleared(*, clean_cycles: int) -> None:
    """TG: kill_switch авто-снят после N чистых reconcile-циклов."""
    try:
        await publish([kill_switch_cleared_notification(clean_cycles=clean_cycles)], actor="execution_reconcile")
    except Exception as e:
        logger.warning("reconciliation_notify_autoclear_failed", error=str(e))


async def run_reconciliation() -> dict:
    """Сверить трейды на Polymarket с execution_orders и обновить bankroll.

    Phase 1 Stage 4: drift detection. Если EXECUTION_ENABLED=false →
    skip целиком (нет live trades для сверки, нет смысла TG спамить).
    """
    stats = {"polymarket_trades": 0, "db_orders": 0, "orphans": 0, "bankroll_updated": False}

    # Skip когда live trading disabled (по default — Settings.reconciliation_skip_when_execution_disabled).
    # Avoid TG спама когда нет live trades для сверки. Override через env для testing.
    settings = get_settings()
    if settings.reconciliation_skip_when_execution_disabled and not settings.execution_enabled:
        logger.debug("reconciliation_skipped_execution_disabled")
        stats["skipped"] = "execution_disabled"
        return stats

    # Phase 1 Stage 4: trades_error tracked отдельно от прочих errors,
    # чтобы 401/timeout от Polymarket API считался drift signal вместо crash.
    trades_error: str | None = None
    try:
        from app.services.wallet_resolver import build_clob_client

        # signature_type/funder/api_creds — из wallet_resolver (единый SSoT).
        client = build_clob_client()

        # 1. Получить все трейды с Polymarket
        try:
            trades = client.get_trades() or []
        except Exception as trades_exc:
            trades = []
            trades_error = str(trades_exc)
            logger.warning("reconciliation_trades_fetch_failed", error=trades_error)
        stats["polymarket_trades"] = len(trades)

        # 2. Получить все non-rejected ордера из БД
        db_orders = await fetch(
            "SELECT order_id, market_id, status, stake FROM execution_orders WHERE status NOT IN ('rejected', 'dry_run')"
        )
        db_order_ids = {r["order_id"] for r in db_orders if r["order_id"]}
        stats["db_orders"] = len(db_orders)

        # 3. Найти orphans — трейды на Polymarket без записи в БД.
        #
        # Polymarket get_trades() возвращает обе стороны:
        # - TAKER: мы инициировали → наш order_id = trade.taker_order_id
        # - MAKER: наш maker GTC ордер был matched другим тейкером →
        #          наш order_id находится в trade.maker_orders[].order_id,
        #          а trade.taker_order_id принадлежит чужому тейкеру.
        #
        # Раньше reconcile сравнивал только taker_order_id, что давало
        # ложные orphans для всех matched maker-ордеров.
        orphans = []
        for t in trades:
            trader_side = (t.get("trader_side") or "").upper()
            our_order_id: str | None = None
            if trader_side == "TAKER":
                our_order_id = t.get("taker_order_id") or None
            elif trader_side == "MAKER":
                # Наш order_id среди maker_orders. У нас всегда один maker per trade
                # (мы не split-fill наш собственный ордер), но цикл — на всякий.
                for m in t.get("maker_orders") or []:
                    mid = m.get("order_id")
                    if mid and mid in db_order_ids:
                        our_order_id = mid
                        break
                if our_order_id is None and t.get("maker_orders"):
                    # Не нашли совпадение → orphan по первому maker_order_id для логирования
                    our_order_id = (t["maker_orders"][0] or {}).get("order_id")

            if our_order_id and our_order_id not in db_order_ids:
                orphans.append(t)

        stats["orphans"] = len(orphans)

        if orphans:
            logger.warning(
                "reconciliation_orphans_found",
                count=len(orphans),
                trade_ids=[o.get("id") for o in orphans],
            )
            await _notify_reconcile_orphans(orphans)

        # 4. Обновить bankroll из реального баланса кошелька
        from app.services.wallet_resolver import fetch_balance, get_signature_type

        wallet_error: str | None = None
        try:
            wallet_balance = await fetch_balance() or 0.0
        except Exception as wallet_exc:
            wallet_balance = 0.0
            wallet_error = str(wallet_exc)
        _ = get_signature_type()  # валидируем, что resolver знает sigtype

        # Frozen = stake ордеров в placed/filled
        frozen = float(
            await fetchval(
                "SELECT COALESCE(SUM(stake), 0) FROM execution_orders WHERE status IN ('pending', 'placed', 'filled')"
            )
            or 0
        )

        total_bankroll = wallet_balance + frozen

        # Обновить strategy_bankroll для live стратегий пропорционально.
        # Phoenix migration (2026-04-30) dropped strategy_bankroll. Этот блок
        # — legacy fallback; в случае missing table → silent skip и продолжаем
        # к drift check (важная Stage 4 функциональность не должна блокироваться
        # отсутствием legacy table).
        try:
            live_strategies = await fetch(
                "SELECT strategy_key, initial_bankroll FROM strategy_bankroll WHERE stage = 'live'"
            )
            if live_strategies:
                n = len(live_strategies)
                per_strategy = total_bankroll / n
                for s in live_strategies:
                    await execute(
                        "UPDATE strategy_bankroll SET bankroll = $1, updated_at = NOW() WHERE strategy_key = $2 AND stage = 'live'",
                        round(per_strategy, 2),
                        s["strategy_key"],
                    )
                stats["bankroll_updated"] = True
        except Exception as bankroll_exc:
            logger.debug(
                "reconciliation_strategy_bankroll_skip",
                error=str(bankroll_exc),
            )

        # Phase 1 Stage 4: strike pattern на drift.
        # drift = orphans > 0 ИЛИ wallet API недоступен ИЛИ trades feed down.
        detected, reason = _drift_detected(
            orphans=len(orphans),
            wallet_error=wallet_error,
            trades_error=trades_error,
        )
        if detected:
            await _handle_drift_strike(reason=reason, stats=stats)
        else:
            await _reset_drift_strikes()
            await _maybe_auto_clear_kill_switch()

        logger.info(
            "reconciliation_complete",
            wallet_balance=wallet_balance,
            frozen=frozen,
            total_bankroll=total_bankroll,
            orphans=len(orphans),
            drift_detected=detected,
            drift_reason=reason if detected else None,
        )

    except Exception as e:
        logger.error("reconciliation_error", error=str(e))
        stats["error"] = str(e)
        # Цикл не завершился чисто → разорвать clean-streak (как drift), иначе
        # auto-clear мог бы накопить N через crash-разрывы (не consecutive).
        from app.services.kill_switch_clear import reset_clean_streak

        await reset_clean_streak(await get_redis(), RECON_CLEAN_STREAK_KEY)
        await _notify_reconcile_error(str(e))

    return stats


def orphans_notification(orphans: list) -> Notification:
    """Сделки на бирже, которых нет в БД."""
    ids = ", ".join(str(o.get("id", "?"))[:12] for o in orphans[:5])
    extra = f" и ещё {len(orphans) - 5}" if len(orphans) > 5 else ""
    return Notification(
        severity=Severity.ACTION_REQUIRED,
        subject=_SUBJECT,
        kind="reconciliation_orphans",
        headline=f"Сделок на бирже без записи в базе: {len(orphans)}",
        what_happened=[
            f"На Polymarket есть {len(orphans)} сделок, которых нет в execution_orders.",
            f"ID: {ids}{extra}.",
        ],
        # Сумму не считаем: она есть в данных, но эти сделки по определению
        # неизвестного происхождения — приписывать им цену бездействия значит
        # утверждать то, чего не знаем (§3).
        what_if_ignored="Пока расхождение держится, сверка копит страйки и может остановить торговлю.",
        what_to_do=(
            "Сверь эти сделки с логами размещения: обычно это ордер, прошедший на бирже, "
            "когда запись в БД не доехала по таймауту."
        ),
        actions=[],
        evidence=[f"orphan_count={len(orphans)}"],
    )


async def _notify_reconcile_orphans(orphans: list) -> None:
    """Уведомить о трейдах на Polymarket без записи в БД."""
    try:
        await publish([orphans_notification(orphans)], actor="execution_reconcile")
    except Exception as e:
        logger.warning("reconcile_notify_orphans_failed", error=str(e))


async def _notify_reconcile_error(error: str) -> None:
    """Уведомить об ошибке reconciliation."""
    try:
        await publish(
            [
                Notification(
                    severity=Severity.ACTION_REQUIRED,
                    subject=_SUBJECT,
                    kind="reconciliation_error",
                    headline="Сверка позиций упала с ошибкой",
                    what_happened=[
                        "Прогон сверки не завершился, расхождения за этот цикл не проверены.",
                        f"Ошибка: {error[:200]}",
                    ],
                    what_if_ignored=(
                        "Пока сверка не работает, расхождение между биржей и базой останется незамеченным."
                    ),
                    what_to_do="Посмотри логи execution_reconcile: чаще всего это недоступный wallet API или БД.",
                    actions=[],
                    evidence=[f"error={error[:120]}"],
                )
            ],
            actor="execution_reconcile",
        )
    except Exception as e:
        logger.warning("reconcile_notify_error_failed", error=str(e))
