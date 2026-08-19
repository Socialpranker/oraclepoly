"""Risk daemon — heart-beat dead-man для OraclePoly. Phase 1 Stage 5.

Запускается как отдельный Docker service (oraclepoly-risk-daemon). НЕ
зависит от FastAPI lifespan. Использует:
- asyncpg для DB reads (read-only по replicate-spirit, real ACL — Stage 8 follow-up).
- redis-py для kill_switch + heartbeats.
- structlog для логирования.
- httpx для TG alerts.

Никогда не пишет в БД, не размещает ордера. Только: read state, evaluate,
alert, SET kill_switch.

Запуск:
  python scripts/risk_daemon.py --loop          # production
  python scripts/risk_daemon.py --check-once    # dev/CI

См. docs/superpowers/plans/2026-05-risk-engine-hardening/05-risk-daemon-deadman.md.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime

import structlog

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.cache import close_redis, get_redis, init_redis  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.peak_bank import get_peak_bank  # noqa: E402
from app.services.database import close_db, fetch, fetchval, init_db  # noqa: E402

logger = structlog.get_logger(__name__)

# Redis keys (mirror Stage 4 reconciliation + heart-beat protocol)
DAEMON_HEARTBEAT_KEY = "bot:risk_daemon_alive"
MAIN_HEARTBEAT_KEY = "bot:risk_heartbeat"
KILL_SWITCH_KEY = "bot:kill_switch"
RECON_STRIKES_KEY = "bot:reconciliation_strikes"
DAEMON_PAUSED_DETECTORS_KEY = "bot:daemon_paused_detectors"


# --- Heart-beat -----------------------------------------------------------


async def write_heartbeat() -> None:
    """Каждый цикл daemon записывает bot:risk_daemon_alive=now с TTL 300s.
    Main process читает этот ключ; stale > threshold → main SET kill_switch."""
    redis = await get_redis()
    if redis is None:
        return
    settings = get_settings()
    await redis.setex(
        DAEMON_HEARTBEAT_KEY,
        settings.risk_daemon_main_stale_threshold_sec,
        datetime.now(UTC).isoformat(),
    )


# --- Checks ---------------------------------------------------------------


async def check_main_alive() -> tuple[bool, str]:
    """True если bot:risk_heartbeat (пишется main process) свежее N сек.

    Returns (False, reason) если main process мёртв либо heartbeat malformed.
    Если Redis недоступен — возвращаем (True, "") (conservative: без данных
    daemon не знает, жив main или нет).
    """
    redis = await get_redis()
    if redis is None:
        return True, ""
    settings = get_settings()
    raw = await redis.get(MAIN_HEARTBEAT_KEY)
    if not raw:
        return False, "main_heartbeat_missing"
    try:
        last = datetime.fromisoformat(raw)
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        age = (datetime.now(UTC) - last).total_seconds()
        if age > settings.risk_daemon_main_stale_threshold_sec:
            return False, f"main_heartbeat_stale:age={age:.0f}s"
    except (ValueError, TypeError):
        return False, "main_heartbeat_malformed"
    return True, ""


async def check_exposure(bankroll_usd: float | None) -> tuple[bool, str]:
    """Sum live open stakes vs hard cap. >cap → kill.

    bankroll_usd=None (peak_bank потерян: рестарт/евикт Redis) → чек без
    знаменателя = самоделение → ложный kill (#621). Пропускаем с debounced warn.
    """
    if bankroll_usd is None:
        await _warn_once_per_hour(
            "exposure_no_denominator",
            "⚠️ RISK DAEMON: peak_bank недоступен, exposure-чек пропущен",
        )
        return True, ""
    settings = get_settings()
    total_open = await fetchval(
        """
        SELECT COALESCE(SUM(stake), 0)
          FROM execution_orders
         WHERE status IN ('placed', 'filled', 'pending')
           AND execution_mode != 'virtual'
        """
    ) or 0.0
    # max(bankroll, 1) защищает от deg division.
    exposure_pct = float(total_open) / max(bankroll_usd, 1.0)
    cap = settings.risk_daemon_exposure_hard_cap_pct
    if exposure_pct > cap:
        return False, f"exposure_hard_cap:{exposure_pct:.1%}>{cap:.0%}"
    return True, ""


async def check_daily_pnl_mtm(bankroll_usd: float | None) -> tuple[bool, str]:
    """Defense-in-depth: hard kill при daily realized+mtm PnL < -5% bankroll.

    Soft check в execution_safety работает на -3%. Daemon catches drift case
    когда soft check уже пропустил (race) или открытые позиции просели глубже.

    bankroll_usd=None (peak_bank потерян) → нет надёжного знаменателя для
    threshold, тот же класс риска, что и check_exposure (#621). Пропускаем.
    """
    if bankroll_usd is None:
        await _warn_once_per_hour(
            "daily_pnl_no_denominator",
            "⚠️ RISK DAEMON: peak_bank недоступен, daily_pnl-чек пропущен",
        )
        return True, ""
    settings = get_settings()
    rows = await fetch(
        """
        SELECT
          (SELECT COALESCE(SUM(net_pnl), 0)
             FROM execution_orders
            WHERE execution_mode != 'virtual'
              AND status = 'settled'
              AND settled_at >= date_trunc('day', now() AT TIME ZONE 'UTC')) AS realized,
          (SELECT COALESCE(SUM(unrealized_pnl_last), 0)
             FROM execution_orders
            WHERE status IN ('placed', 'filled')
              AND settled_at IS NULL
              AND mtm_updated_at > now() - INTERVAL '5 minutes') AS mtm
        """
    )
    if not rows:
        return True, ""
    realized = float(rows[0]["realized"] or 0)
    mtm = float(rows[0]["mtm"] or 0)
    combined = realized + mtm
    threshold = -bankroll_usd * settings.risk_daemon_daily_loss_hard_kill_pct
    if combined < threshold:
        return False, (
            f"daily_loss_hard_kill:realized=${realized:.2f}+mtm=${mtm:.2f}"
            f"=${combined:.2f}<threshold=${threshold:.2f}"
        )
    return True, ""


async def check_recon_strikes() -> tuple[bool, str]:
    """Read bot:reconciliation_strikes (Stage 4). ≥threshold → kill confirmed."""
    redis = await get_redis()
    if redis is None:
        return True, ""
    settings = get_settings()
    raw = await redis.get(RECON_STRIKES_KEY)
    if raw is None:
        return True, ""
    try:
        strikes = int(raw)
    except (TypeError, ValueError):
        return True, ""
    if strikes >= settings.reconciliation_strike_threshold:
        return False, f"recon_strikes:{strikes}"
    return True, ""


async def check_anomalies() -> tuple[bool, str]:
    """Spike detection: > N orders/min (сумма) или > M rejections/min (per-detector) → anomaly.

    Считаем ТОЛЬКО ордера live-детекторов (detector_state.stage='live'). Kill за
    runaway-loop взводит глобальный kill_switch, который останавливает живую
    торговлю — поэтому смотреть он должен на реальные (live) ордера, а не на
    бумажную virtual/shadow-симуляцию. Инцидент 2026-06-29: virtual-детектор
    D38 плодит 12-21 rejected-ордеров/мин на :03 (execution_mode='rejected', НЕ
    'virtual' — поэтому `execution_mode != 'virtual'` НЕ помог бы) → ложный
    anomaly-kill блокировал live D3. JOIN к detector_state отсекает paper trades.

    Атрибуция по детектору (audit топ-6, 2026-07-02): раньше COUNT(*) без
    GROUP BY → шум ОДНОГО детектора глушил весь live-банк глобальным
    kill_switch. Теперь GROUP BY eo.detector:
      - Суммарный order-rate > threshold → глобальный runaway, kill (нет
        смысла целиться в один детектор — система в целом теряет контроль).
      - ≥2 детектора одновременно шумят rejections → тоже глобальный kill
        (паттерн опаснее одиночного шумного детектора — может быть системный
        сбой, а не detector-specific баг).
      - Ровно 1 детектор-нарушитель → изолируем через Redis-паузу
        (bot:daemon_paused_detectors), банк не глушим. Демон не пишет в БД
        (архитектурный контракт) — пауза читается execution_safety.
    """
    settings = get_settings()
    rows = await fetch(
        """
        SELECT eo.detector,
               COUNT(*) FILTER (WHERE eo.created_at > now() - INTERVAL '1 minute') AS recent,
               COUNT(*) FILTER (WHERE eo.created_at > now() - INTERVAL '1 minute'
                                  AND eo.status = 'rejected') AS rejected_recent
          FROM execution_orders eo
          JOIN detector_state ds ON ds.detector = eo.detector
         WHERE eo.created_at > now() - INTERVAL '5 minutes'
           AND ds.stage = 'live'
         GROUP BY eo.detector
        """
    )
    offenders: list[tuple[str, str]] = []
    total_recent = 0
    for r in rows:
        total_recent += int(r["recent"] or 0)
        if int(r["rejected_recent"] or 0) > settings.risk_daemon_anomaly_rejections_per_min:
            offenders.append((r["detector"], f"rejections:{r['rejected_recent']}/min"))
    if total_recent > settings.risk_daemon_anomaly_orders_per_min:
        return False, f"anomaly_high_order_rate:{total_recent}/min"  # суммарный runaway → глобальный kill
    if len(offenders) >= 2:
        return False, "anomaly_multi_detector:" + ",".join(d for d, _ in offenders)
    if len(offenders) == 1:
        det, why = offenders[0]
        await _pause_detector_via_redis(det, f"anomaly_{why}")
        return True, ""  # виновник изолирован, банк не глушим
    return True, ""


async def _pause_detector_via_redis(detector: str, reason: str) -> None:
    """Изолировать шумный детектор через Redis-set — демон НЕ пишет в БД
    (архитектурный контракт). execution_safety.check_daemon_pause читает этот
    set в evaluate() и блокирует новые live-ордера ТОЛЬКО этого детектора."""
    redis = await get_redis()
    if redis is None:
        return
    if not await redis.sismember(DAEMON_PAUSED_DETECTORS_KEY, detector):
        await redis.sadd(DAEMON_PAUSED_DETECTORS_KEY, detector)
        await redis.set(f"bot:daemon_paused_reason:{detector}", reason)
        await _tg_send_with_button(
            f"⏸ RISK DAEMON: детектор {detector} на паузе ({reason}).",
            {"text": "▶️ Снять паузу", "callback_data": f"unlock_detector:{detector}"},
        )


# --- Bankroll source ------------------------------------------------------


async def get_bankroll_for_daemon() -> float | None:
    """Daemon bankroll source.

    Не дёргаем external API (wallet_resolver.fetch_balance) — daemon должен
    быть credentials-light и не зависеть от Polymarket API.

    Source: Redis `bot:risk:peak_bank_usd` — MtM equity, пишется mtm-refresh
    cron каждую минуту (wallet_free + frozen_stakes + unrealized_pnl). Это
    настоящий банк, независимый от открытых позиций.

    None (peak_bank потерян: рестарт/евикт Redis): раньше здесь был fallback
    на SUM(stake) live placed/filled — тот же числитель, что и у check_exposure,
    то есть самоделение (#621 сменил знаменатель, не источник; баг остался при
    отсутствии peak_bank). Теперь возвращаем None — вызывающая сторона
    (check_exposure/check_daily_pnl_mtm) пропускает bankroll-зависимые чеки
    вместо ложного kill. mtm-refresh пересеет peak_bank в течение минуты.
    """
    peak_bank = await get_peak_bank()
    if peak_bank is not None and peak_bank > 0:
        return float(peak_bank)
    return None


# --- Evaluate + kill ------------------------------------------------------


async def _tg_send(text: str) -> None:
    """Direct TG send без main process notifications dependency."""
    import httpx

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    admin_ids_str = os.getenv("ADMIN_TELEGRAM_IDS", "")
    if not bot_token or not admin_ids_str:
        return
    admin_ids = [a.strip() for a in admin_ids_str.split(",") if a.strip()]

    async with httpx.AsyncClient(timeout=10) as client:
        for admin_id in admin_ids:
            try:
                await client.post(
                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                    json={"chat_id": admin_id, "text": text, "parse_mode": "HTML"},
                )
            except Exception as e:
                logger.warning("daemon_tg_send_failed", admin=admin_id, error=str(e))


async def _tg_send_with_button(text: str, button: dict) -> None:
    """Вариант _tg_send с inline-кнопкой (reply_markup). Прямой TG send,
    без зависимости от main process — тот же credentials-light подход,
    что и _tg_send.

    button: {"text": "...", "callback_data": "..."}
    """
    import httpx

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    admin_ids_str = os.getenv("ADMIN_TELEGRAM_IDS", "")
    if not bot_token or not admin_ids_str:
        return
    admin_ids = [a.strip() for a in admin_ids_str.split(",") if a.strip()]

    async with httpx.AsyncClient(timeout=10) as client:
        for admin_id in admin_ids:
            try:
                await client.post(
                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                    json={
                        "chat_id": admin_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "reply_markup": {"inline_keyboard": [[button]]},
                    },
                )
            except Exception as e:
                logger.warning("daemon_tg_send_with_button_failed", admin=admin_id, error=str(e))


async def _warn_once_per_hour(key: str, text: str) -> None:
    """Debounced warn: bankroll-знаменатель потерян (peak_bank None). Раз в час,
    не на каждый цикл демона (check_interval секунды) — иначе TG-спам."""
    redis = await get_redis()
    if redis and await redis.set(f"bot:risk:warn:{key}", "1", nx=True, ex=3600):
        await _tg_send(text)


async def _send_kill_alert(check_name: str, reason: str) -> None:
    """TG: kill_switch ВЗВЕДЁН (торговля остановлена). Кнопка unlock (audit
    топ-6, 2026-07-02) — раньше снять kill можно было только SSH+redis-cli."""
    await _tg_send_with_button(
        f"🛑 <b>RISK DAEMON: KILL SWITCH</b>\n"
        f"Check: <code>{check_name}</code>\n"
        f"Reason: <code>{reason}</code>\n"
        f"Trading paused. Investigate перед unlock:",
        {"text": "🔓 Unlock (снять kill)", "callback_data": f"unlock:{check_name}"},
    )


async def evaluate_and_kill() -> dict:
    """Run all checks. Если any breach → SET kill_switch (preserve first cause).

    Skip когда EXECUTION_ENABLED=false (по default). Heartbeat пишется
    всегда — нужно мониторить daemon health independent от execution.
    """
    settings = get_settings()
    if (
        settings.risk_daemon_skip_when_execution_disabled
        and not settings.execution_enabled
    ):
        return {"status": "skipped", "reason": "execution_disabled"}

    bankroll = await get_bankroll_for_daemon()
    if bankroll is not None and bankroll <= 0:
        return {"status": "no_live_exposure"}

    # bankroll может быть None (peak_bank потерян) — main_alive/recon/anomalies
    # не зависят от bankroll и продолжают проверяться; exposure/daily_pnl сами
    # skip'ают себя (см. check_exposure/check_daily_pnl_mtm) без ложного kill.
    # Запускаем checks параллельно для скорости.
    results = await asyncio.gather(
        check_main_alive(),
        check_exposure(bankroll),
        check_daily_pnl_mtm(bankroll),
        check_recon_strikes(),
        check_anomalies(),
    )
    names = ("main_alive", "exposure", "daily_pnl_hard", "recon_strikes", "anomalies")

    for name, (ok, reason) in zip(names, results, strict=True):
        if not ok:
            redis = await get_redis()
            if redis:
                # Не перетираем existing kill_switch — preserve first cause
                existing = await redis.get(KILL_SWITCH_KEY)
                if not existing:
                    await redis.set(KILL_SWITCH_KEY, f"daemon:{name}:{reason}")
                    await _send_kill_alert(name, reason)
                    logger.error("daemon_kill_set", check=name, reason=reason)
                else:
                    logger.warning(
                        "daemon_kill_skipped_already_set",
                        existing=existing,
                        new=reason,
                    )
            return {"status": "kill", "check": name, "reason": reason}

    return {"status": "ok", "bankroll": bankroll}


# --- Loop -----------------------------------------------------------------

# Авто-снятие kill_switch НЕ делается из daemon: его причины (exposure/daily_pnl
# /anomalies) либо зависят от потенциально stale bankroll-знаменателя, либо суть
# код-фолты. Авто-снимается ТОЛЬКО reconciliation_drift из reconcile-джобы.


async def main_loop() -> None:
    """Production loop: init connections → while True (heartbeat + checks)."""
    settings = get_settings()
    await init_redis(settings.redis_url)
    await init_db()
    try:
        logger.info("risk_daemon_started", interval=settings.risk_daemon_check_interval_sec)
        while True:
            try:
                await write_heartbeat()
                result = await evaluate_and_kill()
                logger.info("daemon_check_complete", **result)
            except Exception as e:
                logger.error("daemon_check_error", error=str(e))
            await asyncio.sleep(settings.risk_daemon_check_interval_sec)
    finally:
        await close_db()
        await close_redis()


async def check_once() -> dict:
    """One-shot для CI / healthcheck."""
    settings = get_settings()
    await init_redis(settings.redis_url)
    await init_db()
    try:
        await write_heartbeat()
        return await evaluate_and_kill()
    finally:
        await close_db()
        await close_redis()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="Production: continuous loop")
    parser.add_argument("--check-once", action="store_true", help="One-shot check, exit")
    args = parser.parse_args()

    if args.loop:
        asyncio.run(main_loop())
    elif args.check_once:
        result = asyncio.run(check_once())
        print(result)
        sys.exit(0 if result.get("status") in ("ok", "skipped", "no_live_exposure") else 1)
    else:
        parser.print_help()
        sys.exit(1)
