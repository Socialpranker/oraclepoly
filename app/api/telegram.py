"""
Oracle Backend - Telegram Bot Webhook API

Handles incoming updates from Telegram Bot API via webhook.
Supports both bot commands and Telegram Mini App interactions.
"""

import hashlib
import hmac as hmac_mod
import ipaddress
import json
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.analytics.action_gates import ActionRequest, check_gates, snapshot_hash
from app.analytics.advisory_buttons import ACK, INVESTIGATE, REVIEW, parse_action_callback, parse_advisory_callback
from app.core.auth import check_admin
from app.core.config import get_settings
from app.core.rate_limiter import limiter
from app.services.action_executor import execute_action
from app.services.database import execute, fetchrow, transaction
from app.services.detector_metrics import (
    compute_promotion_gates,
    evaluate_promote_readiness,
    fetch_backtest_map,
    fetch_shadow_signals_7d,
    fetch_virtual_metrics_7d,  # noqa: F401 — module attr patched в tests/test_tg_promote_validation.py
)

logger = structlog.get_logger()
router = APIRouter()


# --- Pydantic Models for Telegram Updates ---


class TelegramUser(BaseModel):
    id: int
    is_bot: bool = False
    first_name: str
    last_name: str | None = None
    username: str | None = None
    language_code: str | None = None


class TelegramChat(BaseModel):
    id: int
    type: str
    title: str | None = None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None


class TelegramMessage(BaseModel):
    message_id: int
    from_: TelegramUser | None = Field(None, alias="from")
    chat: TelegramChat
    date: int
    text: str | None = None
    reply_markup: dict | None = None  # inline-клавиатура; нужна impcard-квитанции (перерисовка)

    model_config = {"populate_by_name": True}


class CallbackQuery(BaseModel):
    id: str
    from_: TelegramUser | None = Field(None, alias="from")
    message: TelegramMessage | None = None
    data: str | None = None

    model_config = {"populate_by_name": True}


class TelegramUpdate(BaseModel):
    update_id: int
    message: TelegramMessage | None = None
    callback_query: CallbackQuery | None = None


# --- Helper: Send Message via Bot API ---


async def send_telegram_message(chat_id: int, text: str, parse_mode: str = "HTML"):
    """Send a message to a Telegram chat via Bot API."""
    settings = get_settings()
    token = settings.telegram_bot_token

    if not token:
        logger.warning("telegram_bot_token_missing", action="send_message")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
    }

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            logger.info("telegram_message_sent", chat_id=chat_id)
        except httpx.HTTPError as e:
            logger.error("telegram_send_failed", chat_id=chat_id, error=str(e))


async def send_telegram_with_buttons(
    chat_id: int,
    text: str,
    buttons: list[dict],
    parse_mode: str = "HTML",
):
    """Отправить сообщение с inline keyboard.

    buttons: [{"text": "Да ✅", "callback_data": "promote:fast_no_bias"}, ...]
    """
    settings = get_settings()
    token = settings.telegram_bot_token
    if not token:
        logger.warning("telegram_bot_token_missing", action="send_with_buttons")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "reply_markup": {
            "inline_keyboard": [[btn] for btn in buttons],
        },
    }

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            logger.info("telegram_buttons_sent", chat_id=chat_id)
        except httpx.HTTPError as e:
            logger.error("telegram_buttons_send_failed", chat_id=chat_id, error=str(e))


async def send_telegram_with_keyboard_rows(
    chat_id: int,
    text: str,
    keyboard_rows: list[list[dict]],
    parse_mode: str = "HTML",
) -> bool:
    """Отправить сообщение с inline keyboard из готовых рядов.

    В отличие от send_telegram_with_buttons (1 кнопка в ряд) принимает разметку
    рядов как есть: [[btn, btn, btn], ...]. Нужно weekly Improvement brief
    (Компонент 5): 3 карточки × 3 кнопки-действия.

    Возвращает True только при подтверждённой доставке (2xx от Bot API),
    False на отсутствующем токене или HTTPError — вызывающий код (agent_runs
    tg_sent) должен различать реальную доставку от проглоченной ошибки.
    """
    settings = get_settings()
    token = settings.telegram_bot_token
    if not token:
        logger.warning("telegram_bot_token_missing", action="send_with_keyboard_rows")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "reply_markup": {"inline_keyboard": keyboard_rows},
    }

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            logger.info("telegram_keyboard_rows_sent", chat_id=chat_id)
            return True
        except httpx.HTTPError as e:
            logger.error("telegram_keyboard_rows_send_failed", chat_id=chat_id, error=str(e))
            return False


async def edit_message_reply_markup(chat_id: int, message_id: int, keyboard_rows: list[list[dict]]):
    """Перерисовать inline-клавиатуру сообщения (Bot API editMessageReplyMarkup).

    Квитанция брифа (Компонент 5, решение дизайна №5): после нажатия убираем
    ряд обработанной карточки, остальные оставляем. keyboard_rows=[] снимает
    клавиатуру целиком. Фейл не критичен — toast уже ушёл.
    """
    settings = get_settings()
    token = settings.telegram_bot_token
    if not token:
        return

    url = f"https://api.telegram.org/bot{token}/editMessageReplyMarkup"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "reply_markup": {"inline_keyboard": keyboard_rows},
    }

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            response = await client.post(url, json=payload)
            response.raise_for_status()
        except httpx.HTTPError as e:
            logger.error("telegram_edit_reply_markup_failed", chat_id=chat_id, message_id=message_id, error=str(e))


async def answer_callback_query(callback_query_id: str, text: str | None = None):
    """Ответить на callback_query (убрать спиннер на кнопке)."""
    settings = get_settings()
    token = settings.telegram_bot_token
    if not token:
        return

    url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
    payload: dict = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            await client.post(url, json=payload)
        except httpx.HTTPError as e:
            logger.error("telegram_answer_callback_failed", error=str(e))


# --- Callback Query Handler ---


async def _handle_callback_query(cq: CallbackQuery):
    """Обработать нажатие inline-кнопки."""
    data = cq.data or ""
    user_id = cq.from_.id if cq.from_ else 0

    # Проверить что это admin
    settings = get_settings()
    admin_ids = settings.admin_telegram_ids
    if user_id not in admin_ids:
        await answer_callback_query(cq.id, "⛔ Только для админов")
        return

    logger.info("telegram_callback", user_id=user_id, data=data)

    if data.startswith("promote:"):
        detector = data.split(":", 1)[1]
        try:
            await _evaluate_and_handle_promote(cq, detector, str(user_id))
        except Exception as e:
            logger.error("promote_failed", detector=detector, error=str(e))
            await answer_callback_query(cq.id, f"❌ Ошибка: {e}")

    elif data.startswith("reject:"):
        detector = data.split(":", 1)[1]
        await answer_callback_query(cq.id, f"⏸️ {detector} остаётся в текущей стадии")
        if cq.message:
            await send_telegram_message(
                cq.message.chat.id,
                f"⏸️ <b>{detector}</b> promotion отклонён.",
            )

    elif data.startswith("withdraw:"):
        try:
            await _handle_withdrawal_callback(cq, str(user_id))
        except Exception as e:
            logger.error("withdrawal_callback_failed", data=data, error=str(e))
            await answer_callback_query(cq.id, f"❌ Ошибка: {e}")

    elif data.startswith("unlock:"):
        try:
            from app.core.cache import get_redis
            from app.tasks.notifications import send_admin_message

            redis = await get_redis()
            value = await redis.get("bot:kill_switch") if redis else None
            if redis is not None:
                await redis.delete("bot:kill_switch")
            await answer_callback_query(cq.id, "🔓 Kill switch снят")
            await send_admin_message(f"🔓 Kill switch снят через TG ({data}). Была причина: {value or '—'}")
        except Exception as e:
            logger.error("unlock_kill_switch_failed", data=data, error=str(e))
            await answer_callback_query(cq.id, f"❌ Ошибка: {e}")

    elif data.startswith("unlock_detector:"):
        det = data.split(":", 1)[1]
        try:
            from app.core.cache import get_redis

            redis = await get_redis()
            if redis is not None:
                await redis.srem("bot:daemon_paused_detectors", det)
                await redis.delete(f"bot:daemon_paused_reason:{det}")
            await answer_callback_query(cq.id, f"▶️ {det} снят с паузы")
        except Exception as e:
            logger.error("unlock_detector_failed", detector=det, error=str(e))
            await answer_callback_query(cq.id, f"❌ Ошибка: {e}")

    elif data.startswith("close_pos:"):
        order_db_id = data.split(":", 1)[1]
        try:
            from app.services.live_exit import close_live_position

            ok, msg = await close_live_position(order_db_id, reason="manual_tg")
            await _log_l2_legacy_action(
                "close_position",
                {"order_db_id": order_db_id, "reason": "manual_tg"},
                user_id,
                {"clob_order_id": msg} if ok else {"error": msg},
                ok=ok,
            )
            await answer_callback_query(cq.id, "🔴 SELL отправлен" if ok else f"❌ {msg}")
        except Exception as e:
            logger.error("close_pos_failed", data=data, error=str(e))
            await answer_callback_query(cq.id, f"❌ Ошибка: {e}")

    elif data.startswith("ramp_up:"):
        # Phase 3 Task 2 (audit research-gap): human-gated подтверждение
        # следующей ступени ramp-лесенки. Джоба ramp_check предлагает,
        # человек подтверждает — cap меняется только здесь.
        try:
            _, det, next_cap_str = data.split(":", 2)
            next_cap = float(next_cap_str)
            await execute(
                "UPDATE detector_state SET live_stake_cap_usd = $2, updated_at = now() WHERE detector = $1",
                det,
                next_cap,
            )
            await _log_l2_legacy_action(
                "raise_cap", {"detector": det, "new_cap_usd": next_cap}, user_id, {"new_cap_usd": next_cap}, ok=True
            )
            await answer_callback_query(cq.id, f"✅ {det}: cap поднят до ${next_cap:.0f}")
            if cq.message:
                await send_telegram_message(
                    cq.message.chat.id,
                    f"✅ <b>{det}</b>: live_stake_cap_usd → <b>${next_cap:.0f}</b>",
                )
        except Exception as e:
            logger.error("ramp_up_failed", data=data, error=str(e))
            await answer_callback_query(cq.id, f"❌ Ошибка: {e}")

    elif data.startswith("impcard:"):
        try:
            await _handle_improvement_card_callback(cq)
        except Exception as e:
            logger.error("improvement_card_callback_failed", data=data, error=str(e))
            await answer_callback_query(cq.id, f"❌ Ошибка: {e}")

    elif data.startswith("act:"):
        try:
            await _handle_action_callback(cq)
        except Exception as e:
            logger.error("action_callback_failed", data=data, error=str(e))
            await answer_callback_query(cq.id, f"❌ Ошибка: {e}")

    elif data.startswith("adv:"):
        try:
            await _handle_advisory_callback(cq)
        except Exception as e:
            logger.error("advisory_callback_failed", data=data, error=str(e))
            await answer_callback_query(cq.id, f"❌ Ошибка: {e}")

    else:
        await answer_callback_query(cq.id)


# --- Phase 0 Stage 4: Promote with gate validation ──────────────────────────


async def _evaluate_and_handle_promote(cq: CallbackQuery, detector: str, user_id: str) -> dict[str, Any]:
    """Phase 0 Stage 4: validate gates → audit log → UPDATE detector_state.

    Раньше TG promote callback не проверял gates — любой admin одним тапом
    мог shadow→virtual→live. Сейчас:
      1. Fetch detector_state + virtual_7d metrics
      2. Branch по переходу:
         - shadow→virtual: compute_promotion_gates (3-gate legacy: n_events,
           wr_match, verdict) на основе shadow_signals_7d + backtest map
         - virtual→live: evaluate_promote_readiness (SSoT, общий с
           /v2/lifecycle/live_readiness — 6-gate с Wilson lower bound + N≥30
           кумулятивно от promoted_at + adversarial mandatory; recency 30д-окно
           для ROI/Sortino вместо фиксированного 7д)
      3. INSERT INTO promote_attempts (audit, всегда)
      4. UPDATE detector_state.stage только если decision="READY"
      5. TG response — concrete reason при block

    Returns dict for tests: {"allowed": bool, "decision": str, "next_stage": str|None}.
    """
    # 1. Fetch detector state + adversarial_mirror_pass
    state_row = await fetchrow(
        """
        SELECT stage, promoted_at, adversarial_mirror_pass
          FROM detector_state
         WHERE detector = $1
        """,
        detector,
    )
    if state_row is None:
        await answer_callback_query(cq.id, f"❌ {detector}: detector_state не найден")
        return {"allowed": False, "decision": "DETECTOR_NOT_FOUND", "next_stage": None}

    cur_stage = state_row["stage"]
    stage_transitions = {"shadow": "virtual", "virtual": "live"}
    next_stage = stage_transitions.get(cur_stage)
    if next_stage is None:
        await answer_callback_query(cq.id, f"⚠️ {detector} уже {cur_stage} — нечего promote")
        return {"allowed": False, "decision": "ALREADY_AT_TOP", "next_stage": None}

    settings = get_settings()

    # 2. Branch по переходу — разные criteria для shadow→virtual vs virtual→live
    if cur_stage == "shadow":
        # Shadow → Virtual: honest economics gates v2 (≥3/4).
        # См. docs/shadow_metrics_redesign_2026-05.md — legacy WR-based gates
        # систематически mispredict virtual exec PnL, поэтому в promote-решении
        # используем v2 (net PnL + tail risk + break-even WR).
        from app.services.detector_metrics import fetch_virtual_fingerprints

        shadow_map = await fetch_shadow_signals_7d()
        backtest_map = fetch_backtest_map()
        fingerprint_map = await fetch_virtual_fingerprints()
        result = compute_promotion_gates(
            shadow_data=shadow_map.get(detector),
            backtest=backtest_map.get(detector),
            fingerprint=fingerprint_map.get(detector),
        )
        # Unified verdict shape — gates_v2 + ready_v2.
        verdict = {
            "decision": "READY" if result["ready_v2"] else "BLOCKED",
            "gates": result["gates_v2"],
            "gates_passed": result["gates_v2_passed"],
            "gates_total": result["gates_v2_total"],
        }
    else:
        # Virtual → Live: единый расчёт с /v2/lifecycle/live_readiness (audit:
        # TG раньше судил по fetch_shadow_signals_7d — фиксированное 7д-окно
        # с n_min=30 непроходимо для низкочастотных детекторов типа D3).
        # evaluate_promote_readiness — SSoT: N кумулятивно от promoted_at,
        # экономика (ROI/Sortino) — recency 30д-окно. Ровно тот же расчёт,
        # что и UI-карточка в app/api/bot.py::bot_v2_lifecycle_live_readiness.
        days_virtual = 0
        if state_row["promoted_at"]:
            promoted = state_row["promoted_at"]
            if promoted.tzinfo is None:
                promoted = promoted.replace(tzinfo=UTC)
            days_virtual = (datetime.now(UTC) - promoted).days

        verdict = await evaluate_promote_readiness(
            detector,
            state_row["promoted_at"],
            days_virtual=days_virtual,
            adversarial_pass=state_row["adversarial_mirror_pass"],
            execution_enabled=settings.execution_enabled,
        )

    decision = verdict["decision"]
    allowed = decision == "READY"

    # 3. Audit log — ВСЕГДА, даже при block
    blocked_reason = None
    if not allowed:
        failed = [name for name, g in verdict["gates"].items() if not g.get("passed")]
        blocked_reason = f"{decision}: {','.join(failed)}" if failed else decision

    # Atomic: INSERT promote_attempts + (если allowed) UPDATE detector_state.
    # Без транзакции возможно частичное состояние: attempt записан, stage не сменился.
    async with transaction() as conn:
        await conn.execute(
            """
            INSERT INTO promote_attempts
              (detector, from_stage, to_stage, user_id, gates_decision,
               gates_snapshot, allowed, blocked_reason)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8)
            """,
            detector,
            cur_stage,
            next_stage,
            user_id,
            decision,
            json.dumps(verdict["gates"], default=str),
            allowed,
            blocked_reason,
        )
        if allowed:
            await conn.execute(
                """
                UPDATE detector_state
                   SET stage = $2, promoted_at = now(), updated_at = now()
                 WHERE detector = $1
                """,
                detector,
                next_stage,
            )

    if not allowed:
        # 4a. Block — TG explains
        msg = _format_promote_block_message(detector, cur_stage, next_stage, verdict)
        await answer_callback_query(cq.id, f"⛔ Promote blocked: {decision}")
        if cq.message:
            await send_telegram_message(cq.message.chat.id, msg)
        return {"allowed": False, "decision": decision, "next_stage": next_stage}

    warning = ""
    if next_stage == "live":
        warning = "\n⚠️ <b>Внимание:</b> теперь будут размещаться РЕАЛЬНЫЕ ордера на Polymarket!"
    elif next_stage == "virtual":
        warning = "\n💸 Включён paper execution (full pipeline без реальных $$$)."

    await answer_callback_query(cq.id, f"✅ {detector}: {cur_stage} → {next_stage}")
    if cq.message:
        await send_telegram_message(
            cq.message.chat.id,
            f"✅ <b>{detector}</b>: {cur_stage} → <b>{next_stage}</b>{warning}",
        )
    return {"allowed": True, "decision": decision, "next_stage": next_stage}


def _format_promote_block_message(detector: str, from_stage: str, to_stage: str, verdict: dict) -> str:
    """TG message при promote block — explain caller какой gate fail."""
    lines = [
        f"⛔ <b>Promote blocked</b>: {detector}",
        f"Attempt: <code>{from_stage} → {to_stage}</code>",
        f"Decision: <code>{verdict['decision']}</code>",
        "",
        "<b>Gates:</b>",
    ]
    for name, g in verdict["gates"].items():
        passed = g.get("passed")
        status = "✅" if passed else "❌"
        value = g.get("value", "?")
        lines.append(f"  {status} <code>{name}</code>: {value}")

    if verdict["decision"] == "MANUAL_OVERRIDE_NEEDED":
        lines.extend(
            [
                "",
                "<b>👁 Manual adversarial mirror evaluation needed:</b>",
                "<code>POST /v2/lifecycle/adversarial_mirror</code>",
                f'<code>{{"detector": "{detector}", "pass": true|false, "notes": "..."}}</code>',
                "После — попробуй promote ещё раз.",
            ]
        )
    elif verdict["decision"] == "INSUFFICIENT_DATA":
        n_min = verdict["gates"]["n_min"]
        lines.extend(
            [
                "",
                f"<b>Недостаточно данных:</b> N={n_min['value']} < {n_min['threshold']}.",
                "Подожди ещё shadow / virtual signals.",
            ]
        )

    return "\n".join(lines)


# --- Phase 1 Stage 7: Withdrawal Approval Callback ───────────────────────────


async def _handle_withdrawal_callback(cq: CallbackQuery, user_id: str) -> dict[str, Any]:
    """Process withdraw:approve:<uuid> / withdraw:reject:<uuid>.

    Validates: user in admin_telegram_ids, withdrawal in 'proposed', not expired.
    Approve → status='approved' + publish 'withdrawals:execute' Redis channel.
    Reject  → status='rejected'.

    Returns dict for tests: {"action": str, "result": str, "id": str|None}.
    """
    data = cq.data or ""
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "withdraw":
        await answer_callback_query(cq.id, "❌ Invalid callback data")
        return {"action": "invalid", "result": "bad_format", "id": None}

    action = parts[1]
    withdrawal_id = parts[2]

    settings = get_settings()
    try:
        if int(user_id) not in settings.admin_telegram_ids:
            await answer_callback_query(cq.id, "⛔ Not authorized")
            return {"action": action, "result": "not_authorized", "id": withdrawal_id}
    except ValueError:
        await answer_callback_query(cq.id, "⛔ Not authorized")
        return {"action": action, "result": "not_authorized", "id": withdrawal_id}

    row = await fetchrow(
        "SELECT id, status, expires_at, amount_usd FROM withdrawals WHERE id = $1",
        withdrawal_id,
    )
    if row is None:
        await answer_callback_query(cq.id, "❌ Withdrawal not found")
        return {"action": action, "result": "not_found", "id": withdrawal_id}
    if row["status"] != "proposed":
        await answer_callback_query(cq.id, f"❌ Already {row['status']}")
        return {"action": action, "result": "wrong_status", "id": withdrawal_id}
    if datetime.now(UTC) > row["expires_at"]:
        async with transaction() as conn:
            await conn.execute(
                "UPDATE withdrawals SET status='expired' WHERE id = $1 AND status='proposed'",
                withdrawal_id,
            )
        await answer_callback_query(cq.id, "⏰ Expired")
        return {"action": action, "result": "expired", "id": withdrawal_id}

    if action == "approve":
        async with transaction() as conn:
            await conn.execute(
                """
                UPDATE withdrawals
                   SET status='approved', approved_by=$1, approved_at=NOW()
                 WHERE id=$2 AND status='proposed'
                """,
                user_id,
                withdrawal_id,
            )
        # Best-effort Redis publish — если Redis down, audit log выше уже зафиксирован,
        # worker подберёт row при periodic poll (TODO Stage 7 follow-up).
        try:
            from app.core.cache import get_redis

            redis = await get_redis()
            if redis:
                await redis.publish("withdrawals:execute", withdrawal_id)
        except Exception as e:
            logger.warning("withdrawal_redis_publish_failed", id=withdrawal_id, error=str(e))

        amount = float(row["amount_usd"])
        await answer_callback_query(cq.id, f"✅ Approved ${amount:.2f}")
        return {"action": "approve", "result": "ok", "id": withdrawal_id}

    if action == "reject":
        async with transaction() as conn:
            await conn.execute(
                """
                UPDATE withdrawals
                   SET status='rejected', rejected_by=$1, rejected_at=NOW()
                 WHERE id=$2 AND status='proposed'
                """,
                user_id,
                withdrawal_id,
            )
        await answer_callback_query(cq.id, "❌ Rejected")
        return {"action": "reject", "result": "ok", "id": withdrawal_id}

    await answer_callback_query(cq.id, "❌ Unknown action")
    return {"action": action, "result": "unknown_action", "id": withdrawal_id}


async def _handle_improvement_card_callback(cq: CallbackQuery) -> dict[str, Any]:
    """Кнопки weekly Improvement brief (Компонент 5): impcard:{accept|defer|reject}:{id}:{YYYYMMDD}.

    Меняет status + attention_status одной командой (решение дизайна №1);
    TTL 7 дней от даты брифа в payload — истёк → fail-closed, карточку не трогаем
    (agent_tg_communication.md §7.2). Идемпотентность: WHERE status IN ('open','deferred')
    — повтор/гонка → «уже обработано». Авторизация — на входе _handle_callback_query.

    Returns dict для тестов: {"result": str, "card_id": int | None}.
    """
    from app.analytics.improvement_brief import (
        is_button_expired,
        parse_card_callback,
        resolve_button_effect,
    )
    from app.analytics.improvement_cards import next_attention_status

    parsed = parse_card_callback(cq.data or "")
    if parsed is None:
        logger.warning("improvement_card_bad_payload", data=cq.data)
        await answer_callback_query(cq.id, "❌ Некорректная кнопка")
        return {"result": "bad_format", "card_id": None}
    action, card_id, brief_date = parsed

    if is_button_expired(brief_date, datetime.now(UTC)):
        await answer_callback_query(cq.id, "⏰ Истекло — данные могли измениться")
        return {"result": "expired", "card_id": card_id}

    row = await fetchrow("SELECT attention_status FROM improvement_cards WHERE id = $1", card_id)
    if row is None:
        await answer_callback_query(cq.id, "❌ Карточка не найдена")
        return {"result": "not_found", "card_id": card_id}

    new_status, attention_event = resolve_button_effect(action)
    try:
        new_attention = next_attention_status(row["attention_status"], attention_event)
    except ValueError:
        await answer_callback_query(cq.id, "Уже обработано")
        return {"result": "already_handled", "card_id": card_id}

    result = await execute(
        """
        UPDATE improvement_cards
           SET status = $1, attention_status = $2, updated_at = now()
         WHERE id = $3 AND status IN ('open', 'deferred')
        """,
        new_status,
        new_attention,
        card_id,
    )
    if result == "UPDATE 0":
        await answer_callback_query(cq.id, "Уже обработано")
        return {"result": "already_handled", "card_id": card_id}

    toasts = {
        "accept": f"✅ #{card_id} в работу",
        "defer": f"⏸ #{card_id} отложено до следующего брифа",
        "reject": f"❌ #{card_id} отклонено",
    }
    await answer_callback_query(cq.id, toasts[action])
    await _redraw_brief_keyboard(cq, card_id)
    logger.info(
        "improvement_card_button",
        card_id=card_id,
        action=action,
        new_status=new_status,
        attention=new_attention,
    )
    return {"result": "ok", "card_id": card_id}


def _drop_card_row(keyboard_rows: list[list[dict]], card_id: int) -> list[list[dict]]:
    """Убрать ряд нажатой карточки из клавиатуры брифа.

    Ряд карточки = все его кнопки несут ':{card_id}:' в callback_data
    (формат impcard:{action}:{id}:{date}). Чужие ряды не трогаем.
    """
    marker = f":{card_id}:"
    return [row for row in keyboard_rows if not all(marker in btn.get("callback_data", "") for btn in row)]


async def _redraw_brief_keyboard(cq: CallbackQuery, card_id: int) -> None:
    """Квитанция (решение дизайна №5): перерисовать клавиатуру без ряда карточки.

    reply_markup приходит в callback_query.message от Bot API; если сообщение
    или разметка недоступны — молча пропускаем (toast уже ушёл, повтор безопасен).
    Фейл перерисовки не критичен по той же причине.
    """
    msg = cq.message
    if msg is None or not msg.reply_markup:
        return
    rows = msg.reply_markup.get("inline_keyboard") or []
    remaining = _drop_card_row(rows, card_id)
    if remaining == rows:
        return
    try:
        await edit_message_reply_markup(msg.chat.id, msg.message_id, remaining)
    except Exception as e:
        logger.warning("improvement_card_redraw_failed", card_id=card_id, error=str(e))


# --- Phase 2 C3b: L1-кнопки policy-advisory (adv:{action}:{pid}) ────────────

_ADVISORY_PROPOSAL_SQL = """
SELECT verdict, name, metrics
  FROM agent_runs
 WHERE kind = 'proposal' AND config_hash = $1
 ORDER BY started_at DESC
 LIMIT 1
"""

_LOG_ADVISORY_SQL = """
INSERT INTO agent_actions
    (kind, level, status, actor, payload, payload_hash, idempotency_key, expires_at, effect, due_date)
VALUES ($1, 'L1', 'executed', $2, $3::jsonb, $4, $5, now() + interval '7 days', $6::jsonb, $7)
ON CONFLICT (idempotency_key) DO NOTHING
"""

_INVESTIGATE_CLAIM_SQL = """
INSERT INTO agent_claims (statement, scope, status, evidence_run_ids, verify_policy, verify_halflife_days)
VALUES ($1, $2, 'active', '{}'::bigint[], 'time_halflife', 7)
ON CONFLICT (statement) DO UPDATE SET last_verified_at = now(), updated_at = now()
"""

# C4: baseline в момент клика. Bankroll-weighted SUM(net_pnl)/SUM(stake), НЕ sum-per-signal
# (инцидент D42). Окно — drawdown_window_days из PolicyThresholds.
_BASELINE_ROI_SQL = """
SELECT CASE WHEN SUM(stake) > 0 THEN SUM(net_pnl) / SUM(stake) END AS roi
  FROM execution_orders
 WHERE execution_mode IN ('maker', 'taker')
   AND status = 'settled'
   AND settled_at > now() - ($2 || ' days')::interval
   AND detector = $1
"""

_BASELINE_VERDICT_SQL = """
SELECT verdict
  FROM agent_runs
 WHERE kind = 'dq' AND name = $1
 ORDER BY started_at DESC
 LIMIT 1
"""


async def _log_advisory_action(
    pid: str,
    verdict: str,
    scope: str,
    action: str,
    tg_user_id: int,
    effect: dict,
    due_date: datetime | None = None,
) -> bool:
    """Запись нажатия L1-кнопки в agent_actions (C3b §5; C4 — ожидание при ACK).

    status='executed' = человек нажал кнопку, НЕ bounded-executor (Phase 3).
    expires_at = +7d — косметика для NOT NULL, семантики для журнала не несёт.
    idempotency_key = f"{pid}:{tg_user_id}" → ON CONFLICT дедуп повторного клика.
    due_date заполняется только для ACK с резолвнутой метрикой (C4 §4.2).
    Возвращает False при ON CONFLICT (повтор/гонка).
    """
    payload = {"proposal_hash": pid, "verdict": verdict, "scope": scope, "action": action}
    payload_json = json.dumps(payload, sort_keys=True)
    payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
    status = await execute(
        _LOG_ADVISORY_SQL,
        f"advisory_{action}",
        str(tg_user_id),
        payload_json,
        payload_hash,
        f"{pid}:{tg_user_id}",
        json.dumps(effect),
        due_date,
    )
    return status.endswith(" 1")


async def _build_ack_expectation(scope: str) -> tuple[dict | None, datetime | None, str | None]:
    """Ожидание для ACK: (expectation, due_date, reason_if_none).

    Baseline измеряется ЗАНОВО в момент клика, а не берётся из proposal.metrics —
    там лежит только текстовый reason, численного baseline нет (спека §2, решение 1).
    Парсинг числа из reason запрещён: формат писался для человека.

    Любой сбой измерения → (None, None, reason): клик всё равно записывается,
    журнал решения человека важнее телеметрии ожидания.
    """
    from app.analytics.decision_expectation import METRIC_NONE, METRIC_ROI, build_expectation, resolve_metric
    from app.analytics.policy_engine import _DEFAULT_PARAMS, PolicyThresholds

    spec = resolve_metric(scope)
    if spec.kind == METRIC_NONE:
        return None, None, spec.reason

    window_days = PolicyThresholds.from_params(_DEFAULT_PARAMS).drawdown_window_days
    try:
        if spec.kind == METRIC_ROI:
            row = await fetchrow(_BASELINE_ROI_SQL, spec.entity, str(window_days))
            baseline = None if row is None or row["roi"] is None else float(row["roi"])
        else:
            row = await fetchrow(_BASELINE_VERDICT_SQL, spec.entity)
            baseline = None if row is None else str(row["verdict"])
    except Exception as e:
        logger.warning("advisory_baseline_measure_failed", scope=scope, error=str(e))
        return None, None, "baseline measurement failed"

    if baseline is None:
        return None, None, f"no baseline data for {spec.kind} of {spec.entity!r}"

    exp = build_expectation(spec, baseline, datetime.now(UTC), window_days)
    due_date = exp.pop("due_date")
    return exp, due_date, None


async def _handle_advisory_callback(cq: CallbackQuery) -> dict[str, Any]:
    """L1-кнопки policy-advisory: adv:{ack|investigate|review}:{pid} (C3b §3.2).

    Авторизация — на входе _handle_callback_query. Дедуп клика — idempotency_key.
    config_hash иммутабелен → нажатие назавтра находит ту же строку.
    """
    parsed = parse_advisory_callback(cq.data or "")
    if parsed is None:
        await answer_callback_query(cq.id, "❌ Некорректная кнопка")
        return {"result": "bad_format", "action": None, "pid": None}
    action, pid = parsed

    row = await fetchrow(_ADVISORY_PROPOSAL_SQL, pid)
    if row is None:
        await answer_callback_query(cq.id, "❌ Proposal не найден")
        return {"result": "not_found", "action": action, "pid": pid}

    verdict = row["verdict"]
    scope = row["name"].removeprefix("policy_engine:")
    tg_user_id = cq.from_.id if cq.from_ else 0

    if action == ACK:
        expectation, due_date, no_exp_reason = await _build_ack_expectation(scope)
        effect: dict[str, Any] = {"acknowledged": True}
        if expectation is not None:
            effect["expectation"] = expectation
        else:
            effect["no_expectation_reason"] = no_exp_reason
        written = await _log_advisory_action(pid, verdict, scope, action, tg_user_id, effect, due_date)
        if not written:
            await answer_callback_query(cq.id, "Уже обработано")
            return {"result": "already_handled", "action": action, "pid": pid}
        await answer_callback_query(cq.id, "✅ Принято")
        await _redraw_advisory_keyboard(cq)
        logger.info("advisory_button", action=action, pid=pid, scope=scope, expectation=expectation is not None)
        return {"result": "ok", "action": action, "pid": pid}

    if action == INVESTIGATE:
        # Claim с ПУСТЫМ evidence_run_ids — намеренно. improvement_cards_monitor
        # фильтрует по непустому evidence и такие claim'ы игнорирует (спека §4.2):
        # Investigate — честный журнал намерения человека разобраться, а НЕ
        # генератор Improvement Card. outside_cards_pipeline=True в логе — явная
        # пометка для будущего читателя, чтобы отсутствие карточки не приняли за баг.
        statement = f"human requested investigate: {verdict} {scope}"
        await execute(_INVESTIGATE_CLAIM_SQL, statement, scope)
        logger.info("advisory_investigate_claim_written", pid=pid, scope=scope, outside_cards_pipeline=True)
        written = await _log_advisory_action(pid, verdict, scope, action, tg_user_id, {"claim_scope": scope})
        if not written:
            await answer_callback_query(cq.id, "Уже обработано")
            return {"result": "already_handled", "action": action, "pid": pid}
        await answer_callback_query(cq.id, "🔍 Отмечено к разбору")
        await _redraw_advisory_keyboard(cq)
        logger.info("advisory_button", action=action, pid=pid, scope=scope)
        return {"result": "ok", "action": action, "pid": pid}

    if action == REVIEW:
        from app.core.advisory_explainer import explain_advisory

        metrics = row["metrics"] if isinstance(row["metrics"], dict) else json.loads(row["metrics"] or "{}")
        explanation = await explain_advisory(
            {"verdict": verdict, "scope": scope, "reason": metrics.get("reason"), "metrics": metrics}
        )
        effect = {"explained": explanation is not None}
        written = await _log_advisory_action(pid, verdict, scope, action, tg_user_id, effect)
        if not written:
            await answer_callback_query(cq.id, "Уже обработано")
            return {"result": "already_handled", "action": action, "pid": pid}
        if explanation is not None and cq.message is not None:
            await send_telegram_message(cq.message.chat.id, f"📋 <b>Разбор</b>\n\n{explanation}")
            await answer_callback_query(cq.id, "📋 Разбор отправлен")
        else:
            await answer_callback_query(cq.id, "📋 Explainer недоступен")
        await _redraw_advisory_keyboard(cq)
        logger.info("advisory_button", action=action, pid=pid, scope=scope, explained=effect["explained"])
        return {"result": "ok", "action": action, "pid": pid}

    await answer_callback_query(cq.id, "❌ Неизвестное действие")
    return {"result": "unknown_action", "action": action, "pid": pid}


# --- Phase 3: bounded L2 executor (кнопка «⚡ Сделать») ─────────────────────

_FETCH_L2_ACTION_SQL = """
SELECT kind, status, expires_at, payload_hash, payload
  FROM agent_actions
 WHERE idempotency_key = $1 AND level = 'L2'
 LIMIT 1
"""

# РЕЗЕРВ намерения ДО побочного эффекта (спека §8 риск 5). Проверки гейтов читают
# снимок строки, поэтому два одновременных клика проходят их оба; закрыть окно может
# только атомарный захват ПЕРЕД вызовом executor'а. RETURNING пусто → строку уже
# забрал другой клик, эффект не применяем. Паттерн — из live_exit.py:52-66, где та же
# гонка закрыта резервом 'exit_pending' перед походом на биржу.
#
# Статус захвата — 'approved' (он есть в CHECK с Phase 0). Спека §4 говорила, что
# 'approved' не нужен, «между одобрением и исполнением нет промежутка» — гонка,
# найденная ревью Task 4, доказала обратное: промежуток есть, и именно в нём
# исполняется побочный эффект. Своего статуса ('executing') CHECK не допускает,
# миграция ради переименования не нужна.
_RESERVE_L2_ACTION_SQL = """
UPDATE agent_actions
   SET status = 'approved'
 WHERE idempotency_key = $1 AND level = 'L2' AND status = 'proposed'
RETURNING id
"""

# Финализация захваченной строки: WHERE status='approved' — её держим только мы.
_CLAIM_L2_ACTION_SQL = """
UPDATE agent_actions
   SET status = 'executed', executed_at = now(), effect = $2::jsonb
 WHERE idempotency_key = $1 AND level = 'L2' AND status = 'approved'
"""

_FAIL_L2_ACTION_SQL = """
UPDATE agent_actions
   SET status = 'failed', executed_at = now(), effect = $2::jsonb
 WHERE idempotency_key = $1 AND level = 'L2' AND status = 'approved'
"""

_L2_DQ_SQL = """
SELECT verdict FROM agent_runs
 WHERE name = 'dq_audit_daily' AND kind = 'dq'
 ORDER BY started_at DESC LIMIT 1
"""

_L2_SNAPSHOT_SQL = "SELECT stage, live_stake_cap_usd FROM detector_state WHERE detector = $1"

# Аудит кнопок, живущих вне advisory-контура (close_pos:, ramp_up:). Полный контракт
# §4.4 к ним неприменим — у них нет parent-proposal со снапшотом; здесь даётся то,
# чего не было вовсе: след в agent_actions с результатом.
_LOG_L2_LEGACY_SQL_OK = """
INSERT INTO agent_actions
    (kind, level, status, actor, payload, payload_hash, idempotency_key, expires_at, executed_at, effect)
VALUES ($1, 'L2', 'executed', $2, $3::jsonb, $4, $5, now(), now(), $6::jsonb)
ON CONFLICT (idempotency_key) DO NOTHING
"""

_LOG_L2_LEGACY_SQL_FAIL = """
INSERT INTO agent_actions
    (kind, level, status, actor, payload, payload_hash, idempotency_key, expires_at, executed_at, effect)
VALUES ($1, 'L2', 'failed', $2, $3::jsonb, $4, $5, now(), now(), $6::jsonb)
ON CONFLICT (idempotency_key) DO NOTHING
"""


async def _log_l2_legacy_action(kind: str, payload: dict, tg_user_id: int, effect: dict, ok: bool) -> None:
    """След в agent_actions для кнопок вне advisory-контура. Не бросает."""
    payload_json = json.dumps(payload, sort_keys=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    try:
        await execute(
            _LOG_L2_LEGACY_SQL_OK if ok else _LOG_L2_LEGACY_SQL_FAIL,
            kind,
            str(tg_user_id),
            payload_json,
            snapshot_hash(payload),
            f"legacy:{kind}:{ts}:{tg_user_id}",
            json.dumps(effect, default=str),
        )
    except Exception as e:  # noqa: BLE001 — аудит не должен ронять само действие
        logger.warning("l2_legacy_audit_failed", kind=kind, error=str(e))


async def _current_snapshot(payload: dict) -> dict:
    """Текущее состояние величин, по которым принималось решение (спека §4).

    Состав слепка — только медленно меняющиеся поля (§8 риск 2): цена и время сюда
    не попадают, иначе кнопка отказывала бы почти всегда.
    """
    detector = payload.get("detector")
    if detector is None:
        return {}
    row = await fetchrow(_L2_SNAPSHOT_SQL, detector)
    if row is None:
        return {}
    return {"stage": row["stage"], "live_stake_cap_usd": row["live_stake_cap_usd"]}


async def _handle_action_callback(cq: CallbackQuery) -> dict[str, Any]:
    """Кнопка «⚡ Сделать»: act:{pid} → пять гейтов → executor → receipt (Phase 3).

    Авторизация админа — на входе _handle_callback_query, сюда попадают только админы;
    гейт is_admin=True передаётся явно, чтобы чистый слой оставался полным.
    """
    pid = parse_action_callback(cq.data or "")
    if pid is None:
        await answer_callback_query(cq.id, "❌ Некорректная кнопка")
        return {"result": "bad_format", "pid": None}

    row = await fetchrow(_FETCH_L2_ACTION_SQL, pid)
    if row is None:
        await answer_callback_query(cq.id, "❌ Действие не найдено")
        return {"result": "not_found", "pid": pid}

    payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"] or "{}")

    dq_row = await fetchrow(_L2_DQ_SQL)
    dq_verdict = str(dq_row["verdict"]) if dq_row is not None else "RED"  # нет DQ-run → fail-closed

    snapshot = await _current_snapshot(payload)

    decision = check_gates(
        ActionRequest(
            kind=str(row["kind"]),
            status=str(row["status"]),
            expires_at=row["expires_at"],
            payload_hash=str(row["payload_hash"]),
            is_admin=True,
        ),
        datetime.now(UTC),
        dq_verdict,
        snapshot,
    )
    if not decision.allowed:
        await answer_callback_query(cq.id, decision.text)
        logger.info("l2_gate_rejected", pid=pid, reason=decision.reason)
        return {"result": decision.reason, "pid": pid}

    # Захват намерения ДО побочного эффекта: гейты читали снимок строки, поэтому два
    # одновременных клика проходят их оба. RETURNING пусто → строку забрал другой клик,
    # эффект не применяем (спека §8 риск 5; паттерн live_exit.py).
    reserved = await fetchrow(_RESERVE_L2_ACTION_SQL, pid)
    if reserved is None:
        await answer_callback_query(cq.id, "✅ Уже сделано — повторное нажатие ничего не меняет.")
        logger.info("l2_reserve_lost", pid=pid)
        return {"result": "already_handled", "pid": pid}

    result = await execute_action(str(row["kind"]), payload)

    if result.ok:
        await execute(_CLAIM_L2_ACTION_SQL, pid, json.dumps(result.effect, default=str))
        await answer_callback_query(cq.id, "⚡ Сделано")
        await _redraw_advisory_keyboard(cq)
        logger.info("l2_executed", pid=pid, kind=row["kind"], effect=result.effect)
        return {"result": "executed", "pid": pid}

    await execute(_FAIL_L2_ACTION_SQL, pid, json.dumps({"error": result.error}))
    await answer_callback_query(cq.id, f"⚠️ Не получилось: {result.error}. Ничего не изменено.")
    logger.warning("l2_failed", pid=pid, kind=row["kind"], error=result.error)
    return {"result": "failed", "pid": pid}


async def _redraw_advisory_keyboard(cq: CallbackQuery) -> None:
    """Убрать клавиатуру после действия (advisory = одно сообщение, весь ряд снимаем)."""
    msg = cq.message
    if msg is None or not msg.reply_markup:
        return
    try:
        await edit_message_reply_markup(msg.chat.id, msg.message_id, [])
    except Exception as e:
        logger.warning("advisory_redraw_failed", error=str(e))


# --- Bot Command Handlers ---


async def handle_start_command(message: TelegramMessage):
    """Handle /start command - Welcome message with Mini App link."""
    from html import escape

    user_name = escape(message.from_.first_name) if message.from_ else "User"

    welcome_text = (
        f"👋 <b>Привет, {user_name}!</b>\n\n"
        "🔮 <b>Oracle</b> — ИИ-инструмент для анализа рынков предсказаний Polymarket.\n\n"
        "📊 Что умеет Oracle:\n"
        "• Анализ движений китов (whale tracking)\n"
        "• AI-прогнозы по рынкам\n"
        "• Oracle Score — рейтинг надёжности рынка\n"
        "• Уведомления о резких изменениях\n\n"
        "👇 <b>Нажми кнопку ниже, чтобы открыть приложение!</b>"
    )

    await send_telegram_message(message.chat.id, welcome_text)


async def handle_help_command(message: TelegramMessage):
    """Handle /help command."""
    help_text = (
        "📖 <b>Команды Oracle Bot:</b>\n\n"
        "/start — Приветствие и запуск\n"
        "/help — Список команд\n"
        "/markets — Топ рынков по активности\n"
        "/status — Статус сервиса\n\n"
        "💡 <i>Используй кнопку Menu для полного доступа к приложению.</i>"
    )

    await send_telegram_message(message.chat.id, help_text)


async def handle_status_command(message: TelegramMessage):
    """Handle /status command - Service status."""
    status_text = (
        "✅ <b>Oracle Status:</b>\n\n"
        "🟢 Backend API: Online\n"
        "🟢 AI Engine: Active\n"
        "🟢 Whale Tracker: Monitoring\n\n"
        "🕐 <i>Обновлено только что</i>"
    )

    await send_telegram_message(message.chat.id, status_text)


# --- Command Router ---

COMMAND_HANDLERS = {
    "/start": handle_start_command,
    "/help": handle_help_command,
    "/status": handle_status_command,
}


# --- Webhook Endpoint ---


@router.post("/webhook")
@limiter.limit("200/minute")
async def telegram_webhook(request: Request):
    """
    Receive and process Telegram Bot updates via webhook.

    Telegram sends JSON updates to this endpoint.
    Requires TELEGRAM_BOT_TOKEN to be set.
    """
    settings = get_settings()

    if not settings.telegram_bot_token:
        logger.error("telegram_webhook_no_token")
        raise HTTPException(status_code=503, detail="Bot token not configured")

    # IP allowlist (Telegram CIDRs). Пустой список = bypass (dev only).
    # Uvicorn должен быть с --proxy-headers, чтобы request.client.host был real IP.
    if settings.webhook_ip_whitelist and request.client:
        try:
            client_ip = ipaddress.ip_address(request.client.host)
        except ValueError:
            logger.warning("telegram_webhook_invalid_client_host", host=request.client.host)
            raise HTTPException(status_code=403, detail="Invalid client IP") from None
        if not any(client_ip in ipaddress.ip_network(cidr) for cidr in settings.webhook_ip_whitelist):
            logger.warning("telegram_webhook_ip_rejected", ip=str(client_ip))
            raise HTTPException(status_code=403, detail="IP not in whitelist")

    # Validate webhook secret token (обязательно в production)
    secret_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not settings.telegram_webhook_secret:
        raise HTTPException(status_code=503, detail="Webhook secret not configured")
    if not hmac_mod.compare_digest(secret_header, settings.telegram_webhook_secret):
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    try:
        body = await request.json()
        update = TelegramUpdate.model_validate(body)

        logger.info(
            "telegram_update_received",
            update_id=update.update_id,
            has_message=update.message is not None,
            has_callback=update.callback_query is not None,
        )

        # Handle callback queries (inline keyboard buttons)
        if update.callback_query:
            await _handle_callback_query(update.callback_query)
            return {"ok": True}

        # Process message commands
        if update.message and update.message.text:
            text = update.message.text.strip()
            command = text.split()[0].split("@")[0]  # Handle /start@BotName

            handler = COMMAND_HANDLERS.get(command)
            if handler:
                await handler(update.message)
            else:
                logger.debug("telegram_unknown_command", command=command)

    except Exception as e:
        logger.error("telegram_webhook_error", error=str(e))
        # Always return 200 to Telegram to prevent retries
        pass

    # Telegram expects 200 OK always
    return {"ok": True}


@router.get("/webhook/info")
@limiter.limit("10/minute")
async def webhook_info(request: Request, _admin: dict = Depends(check_admin)):
    """Get current webhook status from Telegram API."""
    settings = get_settings()

    if not settings.telegram_bot_token:
        raise HTTPException(status_code=503, detail="Bot token not configured")

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/getWebhookInfo"

    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(url)
        data = response.json()

    result = data.get("result", {})
    return {
        "ok": data.get("ok"),
        "url_configured": bool(result.get("url")),
        "has_custom_certificate": result.get("has_custom_certificate", False),
        "pending_update_count": result.get("pending_update_count", 0),
        "last_error_date": result.get("last_error_date"),
        "last_error_message": result.get("last_error_message"),
        "max_connections": result.get("max_connections"),
    }


@router.post("/webhook/setup")
@limiter.limit("5/minute")
async def setup_webhook(request: Request, _admin: dict = Depends(check_admin)):
    """
    Set up the Telegram webhook URL.

    Call this once after deployment to register the webhook with Telegram.
    Uses the server's own URL (from Host header or configured domain).
    """
    settings = get_settings()

    if not settings.telegram_bot_token:
        raise HTTPException(status_code=503, detail="Bot token not configured")

    # Determine webhook URL
    webhook_url = "https://example.com/api/telegram/webhook"

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/setWebhook"
    payload = {
        "url": webhook_url,
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": True,
    }
    if settings.telegram_webhook_secret:
        payload["secret_token"] = settings.telegram_webhook_secret

    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(url, json=payload)
        result = response.json()

    logger.info("telegram_webhook_set", url=webhook_url, result=result)
    return {"webhook_url": webhook_url, "telegram_response": result}
