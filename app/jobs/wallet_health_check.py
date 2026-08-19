"""Wallet health check — мониторинг funder balance + allowance каждые 15 мин.

Алерт админам при:
- balance < LIVE_WALLET_LOW_THRESHOLD (default $20) — заканчиваются деньги
- balance == 0 — funder неправильный или USDC выведен
- allowances == 0 — нужно нажать "Approve" в Polymarket UI

State в Redis (TTL 1ч), чтоб не спамить алертами при каждом запуске.
"""

from __future__ import annotations

import structlog

from app.analytics.notification import Notification, Severity
from app.services.notification_bus import publish

logger = structlog.get_logger(__name__)

LOW_BALANCE_THRESHOLD_USD: float = 20.0
ALERT_DEDUP_TTL_SEC: int = 3600

# Человеческие формулировки на каждый вид проблемы: что случилось и что делать.
# Технические строки issues[] остаются для дедупа и логов, владельцу идёт этот текст.
_ISSUE_TEXT: dict[str, tuple[str, str]] = {
    "balance_fetch_failed": (
        "Не удалось узнать баланс кошелька — сам по себе это ещё не значит, что с деньгами что-то не так.",
        "Проверь, отвечает ли Polymarket и верен ли адрес кошелька в настройках.",
    ),
    "zero_balance": (
        "На кошельке ноль долларов. Либо деньги выведены, либо в настройках указан не тот адрес.",
        "Сверь POLYMARKET_ADDRESS и signature_type с кошельком, из которого реально торгуешь.",
    ),
    "low_balance": (
        "Денег на кошельке осталось мало.",
        "Пополни кошелёк — иначе ближайшие сделки не пройдут по размеру.",
    ),
    "zero_allowances": (
        "Кошелёк не разрешил бирже тратить USDC — деньги есть, но списать их она не может.",
        "Нажми Approve/Activate в интерфейсе Polymarket.",
    ),
}


def to_notifications(issues: list[tuple[str, str]], balance: float | None) -> list[Notification]:
    """Проблемы кошелька → Notification (спека §3).

    Одно уведомление на кошелёк, а не на каждую проблему: у пустого баланса и
    нулевых allowance один и тот же subject и, как правило, один поход в интерфейс.

    what_if_ignored здесь МОЖНО заполнить — в отличие от остальных мониторов
    пачки, тут есть фактическая цифра: баланс в долларах. §3 запрещает выдумывать
    цифру, но не запрещает называть настоящую.
    """
    if not issues:
        return []

    lines: list[str] = []
    steps: list[str] = []
    for key, raw in issues:
        text, step = _ISSUE_TEXT.get(key, (raw, ""))
        lines.append(text)
        if step:
            steps.append(step)

    if balance is None:
        cost = None
    else:
        cost = (
            f"Сейчас на кошельке ${balance:.2f}. Пока это не исправлено, "
            "новые сделки не откроются — деньги не потеряются, но и не заработают."
        )

    return [
        Notification(
            severity=Severity.ACTION_REQUIRED,
            subject="infra:wallet",
            kind="wallet_health",
            headline="С кошельком для торговли что-то не так",
            what_happened=lines,
            what_if_ignored=cost,
            what_to_do=" ".join(steps) if steps else "Проверь состояние кошелька в интерфейсе Polymarket.",
            actions=[],
            evidence=[raw for _, raw in issues],
        )
    ]


async def run_wallet_health_check() -> dict:
    """Проверить funder balance + allowance, при проблемах — TG-алерт."""
    from app.core.cache import get_redis
    from app.core.config import get_settings
    from app.services.wallet_resolver import fetch_allowance, fetch_balance, get_funder_address

    settings = get_settings()

    # Skip полностью когда execution disabled — нет смысла проверять balance
    # и спамить 401 errors от Polymarket CLOB API key.
    if not settings.execution_enabled:
        logger.info("wallet_health_skipped", reason="execution_disabled")
        return {"status": "skipped", "reason": "execution_disabled"}

    funder = get_funder_address()

    balance = await fetch_balance()
    allowances = await fetch_allowance()

    status: dict = {
        "funder": funder,
        "balance": balance,
        "allowances": allowances,
        "alerts": [],
    }

    # (issue_key, техническое сообщение). Аннотация раньше врала — стояло list[str],
    # хотя кладутся пары; ruff это не ловит, читающего сбивает.
    issues: list[tuple[str, str]] = []
    if balance is None:
        issues.append(("balance_fetch_failed", f"⚠️ Не удалось получить баланс funder {funder[:8]}..."))
    elif balance == 0:
        issues.append(
            ("zero_balance", f"🚨 funder {funder[:8]}... balance = $0. Проверь POLYMARKET_ADDRESS / signature_type.")
        )
    elif balance < LOW_BALANCE_THRESHOLD_USD:
        issues.append(
            ("low_balance", f"⚠️ funder balance = ${balance:.2f} < ${LOW_BALANCE_THRESHOLD_USD}. Нужно пополнить.")
        )

    if allowances is not None:
        ctf_exchange = "0xE111180000d2663C0091e4f400237545B87B996B"  # CTF Exchange V2
        neg_risk = "0xe2222d279d744050d28e00520010520000310F59"  # Neg Risk CTF Exchange V2
        ctf = allowances.get(ctf_exchange, 0.0)
        neg = allowances.get(neg_risk, 0.0)
        if ctf == 0 and neg == 0 and (balance or 0) > 0:
            issues.append(("zero_allowances", "🚨 USDC allowances = 0. Нажми Approve/Activate в Polymarket UI."))

    # Дедуп алертов: один и тот же issue не шлём чаще раза в час. Дедуп остаётся
    # здесь (решение владельца): он per-issue и точнее конвертного — тот работает
    # по кошельку целиком и не различал бы «баланс кончился» и «allowance ноль».
    r = await get_redis()
    fresh: list[tuple[str, str]] = []
    for issue_key, msg in issues:
        dedup_key = f"wallet_alert:{issue_key}"
        send = True
        if r is not None:
            try:
                if await r.exists(dedup_key):
                    send = False
                else:
                    await r.set(dedup_key, "1", ex=ALERT_DEDUP_TTL_SEC)
            except Exception as e:
                logger.debug("wallet_dedup_error", error=str(e)[:80])

        if send:
            fresh.append((issue_key, msg))

    if fresh:
        # Проблемы, прошедшие дедуп, уходят ОДНИМ сообщением: три отдельных
        # про один кошелёк — шум, от которого уводит §4.
        await publish(to_notifications(fresh, balance), actor="wallet_health_check")
        status["alerts"].extend(key for key, _ in fresh)

    logger.info(
        "wallet_health_check",
        funder=funder[:10] if funder else None,
        balance=balance,
        issues=[k for k, _ in issues],
        alerts_sent=status["alerts"],
    )
    return status
