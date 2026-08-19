"""Redeem job — cron (hourly), забирает выигрыш с resolved live-позиций on-chain.

v2_execution_resolve._settle_filled() пишет pnl/net_pnl в БД как ЕСЛИ БЫ winning
shares выкупились по $1 (см. комментарий в _compute_settlement_pnl) — реального
вызова redeemPositions нигде не было. CTF (ERC1155) токены оставались на
funder-кошельке, USDC физически не поступал.

Использует poly-web3 SDK (ProxyWeb3Service.redeem_all()) — вызов через
Polymarket Relayer (не raw on-chain tx: Relayer платит газ, funder MATIC не нужен).
redeem_all() сам решает, что редимить, по Polymarket Data API
(fetch_redeemable_positions) — наш SQL-запрос ниже используется только для
dry-run отчёта и проверки "есть ли смысл вызывать SDK", не как источник для SDK.

REDEEM_ENABLED=false (default) → dry-run: логируем/алертим кандидатов, ничего не
отправляем on-chain.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from app.analytics.notification import Notification, Severity
from app.core.config import get_settings
from app.core.task_queue import debounce_acquire
from app.services.database import execute, fetch
from app.services.notification_bus import publish

logger = structlog.get_logger(__name__)

_FETCH_CANDIDATES_SQL = """
SELECT eo.id, eo.market_id, eo.stake, eo.outcome, eo.net_pnl, m.condition_id
  FROM execution_orders eo
  JOIN markets m ON m.polymarket_id = eo.market_id
 WHERE eo.execution_mode IN ('maker', 'taker')
   AND eo.status = 'settled'
   AND eo.outcome = 'WIN'
   AND eo.redeem_status IS DISTINCT FROM 'redeemed'
   AND m.condition_id IS NOT NULL
"""

_MARK_REDEEMED_SQL = """
UPDATE execution_orders eo
   SET redeem_status = 'redeemed', redeemed_at = now()
  FROM markets m
 WHERE m.polymarket_id = eo.market_id
   AND m.condition_id = ANY($1::text[])
   AND eo.execution_mode IN ('maker', 'taker') AND eo.outcome = 'WIN'
"""

_MARK_FAILED_SQL = """
UPDATE execution_orders eo
   SET redeem_status = 'failed', redeem_error = $2
  FROM markets m
 WHERE m.polymarket_id = eo.market_id AND m.condition_id = $1
   AND eo.execution_mode IN ('maker', 'taker') AND eo.outcome = 'WIN'
"""

_FETCH_LOSS_SUM_SQL = """
SELECT COALESCE(SUM(net_pnl), 0) AS loss_sum
  FROM execution_orders
 WHERE execution_mode IN ('maker', 'taker')
   AND status = 'settled'
   AND outcome = 'LOSS'
   AND settled_at > $1
"""


def _build_proxy_web3_service():
    """ProxyWeb3Service для POLY_PROXY (Magic) wallet, signature_type=1.

    Восстановлено из app/jobs/auto_redeem_job.py (удалён 2026-04-30, Phase F
    cleanup) — последняя V2-совместимая версия, коммит 448a4469.
    """
    from poly_web3 import ProxyWeb3Service
    from poly_web3.const import RELAYER_URL
    from py_builder_relayer_client.client import RelayClient
    from py_builder_signing_sdk.config import BuilderConfig
    from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds
    from py_clob_client_v2.client import ClobClient

    settings = get_settings()

    clob_client = ClobClient(
        host=settings.polymarket_host,
        key=settings.polymarket_private_key,
        chain_id=settings.polymarket_chain_id,
        signature_type=1,
        funder=settings.polymarket_address,
    )

    builder_creds = BuilderApiKeyCreds(
        key=settings.polymarket_builder_key,
        secret=settings.polymarket_builder_secret,
        passphrase=settings.polymarket_builder_passphrase,
    )
    relay_client = RelayClient(
        relayer_url=RELAYER_URL,
        chain_id=settings.polymarket_chain_id,
        private_key=settings.polymarket_private_key,
        builder_config=BuilderConfig(local_builder_creds=builder_creds),
    )

    return ProxyWeb3Service(clob_client=clob_client, relayer_client=relay_client)


_WATERMARK_KEY = "redeem_notify_watermark"
_DRY_RUN_DEDUP_KEY = "redeem_dry_run_notify_last_count"
_DRY_RUN_DEDUP_TTL_SEC = 6 * 3600


async def _get_watermark() -> datetime:
    from app.core.cache import get_redis

    redis = await get_redis()
    if redis:
        raw = await redis.get(_WATERMARK_KEY)
        if raw:
            return datetime.fromisoformat(raw)
    return datetime.now(UTC) - timedelta(hours=1)


async def _set_watermark(ts: datetime) -> None:
    from app.core.cache import get_redis

    redis = await get_redis()
    if redis:
        await redis.set(_WATERMARK_KEY, ts.isoformat())


def dry_run_notification(candidates: list[dict]) -> Notification:
    """Кандидаты на погашение, пока REDEEM_ENABLED=false.

    FYI: ничего не сломалось, погашение выключено намеренно. Но замороженные
    деньги — факт, и их сумма известна, поэтому она в what_if_ignored.
    """
    count = len(candidates)
    frozen = sum(float(c.get("net_pnl") or 0) for c in candidates)
    return Notification(
        severity=Severity.FYI,
        subject="execution:redeem",
        kind="redeem_dry_run",
        headline=f"Готовы к погашению: {count} позиций",
        what_happened=[
            f"{count} выигранных позиций ждут погашения.",
            "Погашение выключено (REDEEM_ENABLED=false), поэтому ничего не отправлено.",
        ],
        what_if_ignored=(f"{frozen:+.2f}$ остаются на контракте, а не на кошельке." if frozen else None),
        what_to_do="Если это не нарочно — включи REDEEM_ENABLED, и ближайший прогон погасит их.",
        actions=[],
        evidence=[f"candidates={count}", f"frozen_pnl={frozen:+.2f}"],
    )


async def _notify_dry_run(candidates: list[dict]) -> None:
    """Алерт про накопленных dry-run кандидатов, с дедупом по Redis.

    Без дедупа джоба (hourly) шлёт одно и то же сообщение каждый час, пока
    REDEEM_ENABLED=false и кандидаты не меняются — throttle до раза в
    _DRY_RUN_DEDUP_TTL_SEC, но пропускаем throttle если счёт кандидатов
    вырос (эскалацию риска замалчивать нельзя).
    """
    try:
        from app.core.cache import get_redis

        count = len(candidates)
        redis = await get_redis()
        if redis is not None:
            try:
                last_raw = await redis.get(_DRY_RUN_DEDUP_KEY)
                last_count = int(last_raw) if last_raw else None
                if last_count is not None and count <= last_count:
                    return
                await redis.set(_DRY_RUN_DEDUP_KEY, str(count), ex=_DRY_RUN_DEDUP_TTL_SEC)
            except Exception as e:
                logger.debug("redeem_dry_run_dedup_error", error=str(e)[:80])

        await publish([dry_run_notification(candidates)], actor="redeem_job")
    except Exception as e:
        logger.warning("redeem_dry_run_notify_failed", error=str(e)[:200])


def result_notification(*, success_count: int, error_count: int, redeemed_pnl: float, loss_sum: float) -> Notification:
    """Итог погашения — деньги здесь настоящие, уже зачисленные.

    FYI: погашение прошло, делать владельцу нечего. ACTION_REQUIRED только если
    часть батчей упала — тогда деньги остались непогашенными и нужно вмешаться.
    """
    net_sum = redeemed_pnl + loss_sum
    failed = error_count > 0
    lines = [
        f"Погашено батчей: {success_count}" + (f", с ошибкой: {error_count}." if failed else "."),
        f"Выиграно в этом погашении: {redeemed_pnl:+.2f}$.",
        f"Проиграно с прошлого отчёта: {loss_sum:+.2f}$.",
        f"Итого с прошлого отчёта: {net_sum:+.2f}$.",
    ]
    return Notification(
        severity=Severity.ACTION_REQUIRED if failed else Severity.FYI,
        subject="execution:redeem",
        kind="redeem_failed" if failed else "redeem_result",
        headline=("Часть выигрышей не погасилась" if failed else "Выигрыши погашены"),
        what_happened=lines,
        # Цену бездействия называем только когда она известна и реальна: это
        # деньги, застрявшие в непогашенных батчах. При успехе её нет.
        what_if_ignored=(
            f"{error_count} батчей остаются непогашенными — деньги не дошли до кошелька." if failed else None
        ),
        what_to_do=(
            "Посмотри логи redeem_job по упавшим батчам и запусти погашение повторно."
            if failed
            else "Ничего делать не нужно — деньги уже на кошельке."
        ),
        actions=[],
        evidence=[
            f"success={success_count}",
            f"errors={error_count}",
            f"redeemed_pnl={redeemed_pnl:+.2f}",
            f"loss_sum={loss_sum:+.2f}",
        ],
    )


async def _notify_result(result: Any, redeemed_candidates: list[dict]) -> None:
    try:
        success_count = len(result.success_list)
        error_count = len(result.error_list)

        watermark = await _get_watermark()
        redeemed_pnl = sum(float(c["net_pnl"] or 0) for c in redeemed_candidates)
        loss_rows = await fetch(_FETCH_LOSS_SUM_SQL, watermark)
        loss_sum = float(loss_rows[0]["loss_sum"]) if loss_rows else 0.0

        if not redeemed_candidates and loss_sum == 0:
            return

        await publish(
            [
                result_notification(
                    success_count=success_count,
                    error_count=error_count,
                    redeemed_pnl=redeemed_pnl,
                    loss_sum=loss_sum,
                )
            ],
            actor="redeem_job",
        )
        # Watermark двигаем ПОСЛЕ публикации: упади она раньше — и убытки за
        # окно были бы отмечены как показанные, хотя владелец их не увидел.
        await _set_watermark(datetime.now(UTC))
    except Exception as e:
        logger.warning("redeem_result_notify_failed", error=str(e)[:200])


async def _notify_error(error: str) -> None:
    try:
        await publish(
            [
                Notification(
                    severity=Severity.ACTION_REQUIRED,
                    subject="execution:redeem",
                    kind="redeem_error",
                    headline="Погашение выигрышей упало с ошибкой",
                    what_happened=[
                        "Прогон погашения не завершился, выигранные позиции остались непогашенными.",
                        f"Ошибка: {error[:200]}",
                    ],
                    what_if_ignored="Деньги за выигранные позиции не дойдут до кошелька, пока погашение не пройдёт.",
                    what_to_do="Посмотри логи redeem_job: обычно это недоступный CLOB или проблема с ключом кошелька.",
                    actions=[],
                    evidence=[f"error={error[:120]}"],
                )
            ],
            actor="redeem_job",
        )
    except Exception as e:
        logger.warning("redeem_error_notify_failed", error=str(e)[:200])


async def run_redeem() -> dict[str, Any]:
    settings = get_settings()

    candidates = await fetch(_FETCH_CANDIDATES_SQL)
    if not candidates:
        return {"status": "ok", "candidates": 0}

    if not settings.redeem_enabled:
        logger.info(
            "redeem_dry_run",
            candidates=len(candidates),
            condition_ids=[c["condition_id"] for c in candidates],
        )
        await _notify_dry_run(candidates)
        return {"status": "dry_run", "candidates": len(candidates)}

    if not await debounce_acquire("redeem_job_running", ttl=900):
        return {"status": "skipped", "reason": "already_running_or_recent"}

    try:
        svc = _build_proxy_web3_service()
        result = svc.redeem_all(batch_size=10)
    except Exception as e:
        logger.error("redeem_failed", error=str(e)[:200])
        await _notify_error(str(e))
        return {"status": "error", "error": str(e)[:200]}

    if result.success_list:
        # SDK группирует redeem по батчам condition_id (до batch_size за раз) и
        # добавляет в success_list ОДИН результат на весь батч — там нет
        # per-position поля condition_id, поэтому success_list саму по себе
        # смаппить нельзя. error_list, наоборот, даёт per-condition_id гранулярность
        # (объекты с атрибутом .condition_id) — считаем redeemed все наши кандидаты,
        # которые НЕ попали в error_list.
        failed_condition_ids = {err.condition_id for err in result.error_list}
        redeemed_candidates = [c for c in candidates if c["condition_id"] not in failed_condition_ids]
        redeemed_condition_ids = [c["condition_id"] for c in redeemed_candidates]
        if redeemed_condition_ids:
            await execute(_MARK_REDEEMED_SQL, redeemed_condition_ids)
    else:
        redeemed_candidates = []

    for err in result.error_list:
        await execute(_MARK_FAILED_SQL, err.condition_id, err.error[:500])

    await _notify_result(result, redeemed_candidates)

    return {
        "status": "ok",
        "redeemed": len(result.success_list),
        "errors": len(result.error_list),
    }
