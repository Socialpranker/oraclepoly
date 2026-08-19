"""
IPRoyal Proxy Health Check — мониторинг прокси.

Запуск: python scripts/cron_runner.py proxy-health
Расписание: каждые 5 минут (вместе с trade-poll)

Проверяет:
1. Истечение сессии (PROXY_EXPIRES_AT) — алерт за 1 час до конца [каждые 5 мин, без трафика]
2. TCP connect к geo.iproyal.com:12321 — порт доступен [раз в час, без трафика]
3. available_traffic < порога (IPROYAL_TRAFFIC_ALERT_GB) — алерт [раз в 6 часов]
4. API токен невалиден (401/403) — алерт [раз в 6 часов]
5. Auth через реальный прокси-туннель (GET {polymarket_host}/time) — [раз в 5 мин, без трафика]

Проверка 5 (auth probe) отдельна от TCP (2): TCP видит только открыт ли порт,
не проверяет credentials. Инцидент 2026-07-18..21 — `407 Proxy Authentication
Required` на write-ордере (v2-execution.log) при TCP/traffic чеках "ok" в те же
минуты: сбой был именно в auth-рукопожатии прокси-туннеля, не в доступности
порта или трафике. Гоняем пробный запрос ЧЕРЕЗ прокси тем же способом, каким
идут реальные ордера (httpx.Client(http2=True, proxy=...), см.
wallet_resolver.py:106), а не отдельным TCP-connect или прямым запросом к
resi-api.iproyal.com — иначе секундный auth-blip на туннеле остаётся невидим.

Redis ключ `proxy:healthy` (TTL 10 мин):
  "1" — прокси жив, запросы к Polymarket разрешены
  "0" — прокси мёртв, запросы к Polymarket заблокированы (fail-closed)

Алерты edge-triggered (см. _alert_on_edge): сообщение уходит только при СМЕНЕ
состояния условия — один раз при поломке и один раз при восстановлении (✅).
Пока условие остаётся сломанным, джоба молчит. Без этого джоба на 5-минутном
тике слала одно и то же сообщение 288 раз в сутки (жалоба 2026-08-01 на 407-спам).
Состояние держим в Redis БЕЗ TTL: истечение TTL при всё ещё сломанном прокси
выглядело бы как новое событие и вернуло бы спам.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx
import structlog

from app.analytics.notification import Notification, Severity
from app.services.notification_bus import publish

logger = structlog.get_logger(__name__)

IPROYAL_API_URL = "https://resi-api.iproyal.com/v1"
SESSION_EXPIRY_ALERT_HOURS = 1
REDIS_KEY = "proxy:healthy"
REDIS_TTL = 600  # 10 минут
TCP_CHECK_INTERVAL = 3600  # TCP проверка раз в час
API_CHECK_INTERVAL = 21600  # API проверка раз в 6 часов
AUTH_PROBE_INTERVAL = 300  # auth-проверка через прокси-туннель раз в 5 мин (каждый тик джобы)
AUTH_PROBE_TIMEOUT = 10.0  # секунд — короче, чем у реального ордера (30s), не держим джобу


async def run_proxy_health_check() -> dict[str, Any]:
    """Проверить прокси и обновить Redis-флаг proxy:healthy."""
    from app.core.config import get_settings

    settings = get_settings()
    alerts: list[str] = []
    result: dict[str, Any] = {}

    if not settings.polymarket_proxy:
        await _set_proxy_health(False)
        if settings.execution_enabled:
            no_proxy_alert = await _alert_on_edge(
                "no_proxy",
                failing=True,
                fail_msg="⚠️ POLYMARKET_PROXY не задан — execution запросы идут без прокси!",
                ok_msg="✅ POLYMARKET_PROXY снова задан.",
            )
            if no_proxy_alert:
                alerts.append(no_proxy_alert)
                await _send_alerts(alerts)
            return {"status": "no_proxy", "alerts_sent": len(alerts)}
        logger.info("proxy_not_set_execution_disabled_skip_alert")
        return {"status": "no_proxy_execution_off", "alerts_sent": 0}

    # Прокси задан — снимаем состояние "no_proxy", если оно висело с прошлого прогона.
    no_proxy_recovered = await _alert_on_edge(
        "no_proxy",
        failing=False,
        fail_msg="",
        ok_msg="✅ POLYMARKET_PROXY снова задан.",
    )
    if no_proxy_recovered:
        alerts.append(no_proxy_recovered)

    # --- 2. TCP connect (раз в час, без трафика) ---
    proxy_alive = True  # оптимистично если кеш свежий
    tcp_result = await _tcp_check_cached(settings.polymarket_proxy)
    if tcp_result is not None:
        proxy_alive = tcp_result
        result["tcp_checked"] = True
        tcp_alert = await _alert_on_edge(
            "tcp",
            failing=not proxy_alive,
            fail_msg="🔴 Прокси недоступен (TCP) — порт закрыт!\nОбнови сессию на iproyal.com/dashboard",
            ok_msg="✅ Прокси снова доступен по TCP — порт открыт.",
        )
        if tcp_alert:
            alerts.append(tcp_alert)

    # --- 2b. Auth через реальный прокси-туннель (раз в 5 мин, без трафика).
    # Независимо от TCP: порт может быть открыт (proxy_alive=True выше), а
    # credentials туннеля всё равно отвергнуты (407) — TCP connect не проверяет
    # auth. Не гейтим proxy_alive этим чеком (redeem/read не зависят от write-пути),
    # но алертим отдельно, чтобы 407 на живом-по-TCP прокси не оставался невидимым.
    auth_result = await _proxy_auth_check_cached(settings.polymarket_proxy)
    if auth_result is not None:
        result["auth_checked"] = True
        result["proxy_auth_ok"] = auth_result
        auth_alert = await _alert_on_edge(
            "auth",
            failing=not auth_result,
            fail_msg=(
                "🔑 Прокси-туннель отверг авторизацию (407) на реальном CLOB-запросе — "
                "write-ордера будут падать place_error!\nПроверь credentials/сессию на iproyal.com/dashboard"
            ),
            ok_msg="✅ Прокси-туннель снова авторизуется — write-ордера пройдут.",
        )
        if auth_alert:
            alerts.append(auth_alert)

    await _set_proxy_health(proxy_alive)

    # --- 1b. Таймер сессии (каждые 5 мин, бесплатно, алерт не чаще раз в 30 мин).
    # Гейтим по proxy_alive: PROXY_EXPIRES_AT — вручную вбитая дата, легко устаревает;
    # если реальный TCP/API-чек говорит alive, «истекла Nч назад» — ложная тревога
    # (инцидент 2026-07-13: 3 суток ежечасного спама при живом прокси). ---
    if settings.proxy_expires_at:
        expiry_alert = _should_alert_expiry(settings.proxy_expires_at, proxy_alive)
        edge_alert = await _alert_on_edge(
            "expiry",
            failing=expiry_alert is not None,
            fail_msg=expiry_alert or "",
            ok_msg="✅ Прокси-сессия больше не на грани истечения.",
        )
        if edge_alert:
            alerts.append(edge_alert)
            result["session_expiry_alert"] = True

    # --- 3. Трафик через API (раз в 6 часов) ---
    if settings.iproyal_api_token:
        traffic_alerts = await _check_iproyal_api(settings)
        alerts.extend(traffic_alerts)

    if alerts:
        await _send_alerts(alerts)
        result["alerts_sent"] = len(alerts)
        logger.warning("proxy_health_alerts", count=len(alerts))
    else:
        result["alerts_sent"] = 0
        logger.info("proxy_health_ok", auth_ok=result.get("proxy_auth_ok"), alive=proxy_alive)

    # Статус — про здоровье прокси, НЕ про факт отправки сообщения. С edge-triggered
    # дедупом молчание при всё ещё сломанном прокси — норма, и завязка на `alerts`
    # (как было до дедупа, когда алерт летел на каждом тике) отрапортовала бы "ok"
    # при живом 407.
    auth_broken = result.get("proxy_auth_ok") is False
    result["proxy_alive"] = proxy_alive
    result["status"] = "ok" if proxy_alive and not auth_broken else "degraded"
    return result


def _check_session_expiry(expires_at_str: str) -> str | None:
    """Алерт если до истечения сессии ≤ SESSION_EXPIRY_ALERT_HOURS."""
    try:
        expires_at = datetime.fromisoformat(expires_at_str)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        now = datetime.now(UTC)
        delta = expires_at - now
        hours_left = delta.total_seconds() / 3600

        if hours_left < 0:
            return f"🔴 Прокси-сессия истекла {abs(hours_left):.0f}ч назад! Обнови на iproyal.com"
        elif hours_left <= SESSION_EXPIRY_ALERT_HOURS:
            minutes_left = int(delta.total_seconds() / 60)
            return f"⏰ Прокси-сессия истекает через <b>{minutes_left} мин</b>! Обнови на iproyal.com"
    except ValueError as e:
        logger.warning("proxy_expiry_parse_error", error=str(e))
    return None


def _should_alert_expiry(expires_at_str: str, proxy_alive: bool) -> str | None:
    """Expiry-алерт с гейтом по реальному состоянию прокси.

    Инцидент 2026-07-13: PROXY_EXPIRES_AT — вручную вбитая дата в .env, которая
    легко устаревает (продлил сессию, но не обновил дату). Если реальный
    TCP/API-чек говорит, что прокси жив, «истекла Nч назад» — ложная тревога.
    Слать expiry-алерт только когда дата в прошлом И прокси реально недоступен.
    Предупреждение «истекает через N мин» (ещё жив по дате) не гейтим — оно
    полезно как ранний сигнал продлить сессию заранее.
    """
    msg = _check_session_expiry(expires_at_str)
    if msg is None:
        return None
    # Уже истекла (по дате), но реальный чек говорит alive → подавляем спам.
    if proxy_alive and "истекла" in msg:
        logger.info("proxy_expiry_stale_date_suppressed", detail="expires_at в прошлом, но proxy_alive=true")
        return None
    return msg


async def _tcp_check_cached(proxy_url: str) -> bool | None:
    """TCP connect к прокси-хосту, раз в час. None = кеш свежий, не проверяли."""
    from app.core.cache import get_redis

    redis = await get_redis()
    cache_key = "proxy:tcp_checked"

    if redis and await redis.get(cache_key):
        return None  # Проверяли недавно — не трогаем

    alive = await _tcp_connect(proxy_url)

    if redis:
        await redis.set(cache_key, "1", ex=TCP_CHECK_INTERVAL)

    return alive


async def _tcp_connect(proxy_url: str) -> bool:
    """Проверить доступность прокси через TCP без передачи данных."""
    try:
        parsed = urlparse(proxy_url)
        host = parsed.hostname or "geo.iproyal.com"
        port = parsed.port or 12321
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=5.0,
        )
        writer.close()
        await writer.wait_closed()
        return True
    except Exception as e:
        logger.warning("proxy_tcp_failed", error=str(e))
        return False


async def _proxy_auth_check_cached(proxy_url: str) -> bool | None:
    """Auth-проверка через прокси-туннель, раз в AUTH_PROBE_INTERVAL. None = кеш свежий."""
    from app.core.cache import get_redis

    redis = await get_redis()
    cache_key = "proxy:auth_checked"

    if redis and await redis.get(cache_key):
        return None  # Проверяли недавно — не трогаем

    ok = await _proxy_auth_probe(proxy_url)

    if redis:
        await redis.set(cache_key, "1", ex=AUTH_PROBE_INTERVAL)

    return ok


def _build_probe_client(proxy_url: str) -> httpx.AsyncClient:
    """Тот же способ построения прокси-клиента, что и на write-пути ордера
    (wallet_resolver.py:106) — иначе проверяем не тот код, что реально ловит 407.
    """
    return httpx.AsyncClient(http2=True, proxy=proxy_url, timeout=AUTH_PROBE_TIMEOUT)


async def _proxy_auth_probe(proxy_url: str) -> bool:
    """GET {polymarket_host}/time ЧЕРЕЗ прокси-туннель — лёгкий CLOB-эндпоинт без
    подписи/параметров, значит любой не-2xx/не-4xx-от-CLOB ответ или обрыв
    соединения — сбой самого туннеля (auth/сеть прокси), не бизнес-логики CLOB.

    407 — целевой сигнал (Proxy Authentication Required, инцидент 2026-07-18..21).
    Прочие транспортные ошибки тоже трактуем как fail (fail-closed): раз мы не
    можем достучаться до CLOB через туннель, не важно из-за чего конкретно.
    2xx/4xx-НЕ-407 от CLOB (в т.ч. 404 если /time когда-нибудь переедет) значит
    туннель и auth отработали — CLOB вообще ответил, это не наша забота здесь.
    """
    from app.core.config import get_settings

    url = f"{get_settings().polymarket_host}/time"
    try:
        async with _build_probe_client(proxy_url) as client:
            response = await client.get(url)
    except Exception as e:
        logger.warning("proxy_auth_probe_failed", error=str(e)[:200])
        return False

    if response.status_code == 407:
        logger.warning("proxy_auth_probe_407", url=url)
        return False
    return True


async def _check_iproyal_api(settings: Any) -> list[str]:
    """Трафик через IPRoyal API, раз в 6 часов."""
    import httpx

    from app.core.cache import get_redis

    redis = await get_redis()
    cache_key = "proxy:api_checked"
    if redis and await redis.get(cache_key):
        return []

    alerts: list[str] = []
    headers = {"Authorization": f"Bearer {settings.iproyal_api_token}"}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{IPROYAL_API_URL}/me", headers=headers)

            if resp.status_code in (401, 403):
                reason = "невалиден (401)" if resp.status_code == 401 else "отозван (403)"
                token_alert = await _alert_on_edge(
                    "api_token",
                    failing=True,
                    fail_msg=f"🔑 IPRoyal API токен {reason} — обнови IPROYAL_API_TOKEN",
                    ok_msg="✅ IPRoyal API токен снова принимается.",
                )
                if token_alert:
                    alerts.append(token_alert)
                return alerts

            token_ok_alert = await _alert_on_edge(
                "api_token",
                failing=False,
                fail_msg="",
                ok_msg="✅ IPRoyal API токен снова принимается.",
            )
            if token_ok_alert:
                alerts.append(token_ok_alert)

            resp.raise_for_status()
            me = resp.json()
            available_gb: float = float(me.get("available_traffic", 0))

            traffic_alert = await _alert_on_edge(
                "traffic",
                failing=available_gb < settings.iproyal_traffic_alert_gb,
                fail_msg=(
                    f"📉 IPRoyal трафик: осталось <b>{available_gb:.2f} GB</b> "
                    f"(порог: {settings.iproyal_traffic_alert_gb} GB)"
                ),
                ok_msg=f"✅ IPRoyal трафик пополнен: <b>{available_gb:.2f} GB</b> доступно.",
            )
            if traffic_alert:
                alerts.append(traffic_alert)

    except httpx.HTTPError as e:
        logger.warning("iproyal_api_check_failed", error=str(e))

    if redis:
        await redis.set(cache_key, "1", ex=API_CHECK_INTERVAL)

    return alerts


async def _set_proxy_health(alive: bool) -> None:
    """Записать Redis-флаг proxy:healthy."""
    try:
        from app.core.cache import get_redis

        redis = await get_redis()
        if redis:
            await redis.set(REDIS_KEY, "1" if alive else "0", ex=REDIS_TTL)
    except Exception as e:
        logger.warning("proxy_redis_set_failed", error=str(e))


async def is_proxy_healthy() -> bool:
    """Проверить Redis-флаг перед Polymarket запросами. Fail-open если Redis недоступен."""
    try:
        from app.core.cache import get_redis

        redis = await get_redis()
        if not redis:
            return True
        val = await redis.get(REDIS_KEY)
        if val is None:
            return True  # Ещё не проверялся — первый запуск
        return val in ("1", b"1")
    except Exception:
        return True


async def _alert_on_edge(condition: str, failing: bool, fail_msg: str, ok_msg: str) -> tuple[str, bool] | None:
    """Вернуть (текст, failing) ТОЛЬКО при смене состояния условия, иначе None.

    Дедуп по событию, а не по времени: пока условие держится в одном состоянии,
    сообщение не повторяется. Возвращает fail_msg на переходе ok→fail и ok_msg на
    переходе fail→ok.

    Направление перехода возвращается вместе с текстом НАМЕРЕННО: по одному тексту
    отличить поломку от восстановления нельзя, а конверту это различие критично.
    Дедуп шины — 25 часов по ключу (subject, kind), и если «сломалось» и
    «починилось» делят kind, то recovery в течение суток после fail будет молча
    подавлено — владелец узнает о поломке и никогда об устранении.

    Ключ живёт без TTL — при живой проблеме протухание ключа выглядело бы как
    новая поломка и вернуло бы ровно тот спам, ради которого это писалось.

    Redis недоступен → возвращаем fail_msg при поломке (не теряем сигнал), но не
    шлём recovery: без состояния «восстановление» — это не событие, а просто
    отсутствие проблемы, и слать его на каждом тике нельзя.
    """
    key = f"proxy:alert_state:{condition}"
    new_state = "fail" if failing else "ok"

    try:
        from app.core.cache import get_redis

        redis = await get_redis()
    except Exception as e:
        logger.warning("proxy_alert_state_redis_unavailable", condition=condition, error=str(e))
        redis = None

    if not redis:
        return (fail_msg, True) if failing else None

    try:
        raw = await redis.get(key)
    except Exception as e:
        logger.warning("proxy_alert_state_read_failed", condition=condition, error=str(e))
        return (fail_msg, True) if failing else None

    prev_state = raw.decode() if isinstance(raw, bytes) else raw

    if prev_state == new_state:
        return None  # состояние не изменилось — молчим

    try:
        await redis.set(key, new_state)
    except Exception as e:
        logger.warning("proxy_alert_state_write_failed", condition=condition, error=str(e))

    if failing:
        return (fail_msg, True)
    # Первый в истории прогон (prev_state is None) при здоровом прокси — не событие.
    if prev_state is None:
        return None
    return (ok_msg, False)


def to_notifications(alerts: list[tuple[str, bool]]) -> list[Notification]:
    """Edge-переходы прокси → Notification (спека §3).

    Поломки и восстановления РАЗДЕЛЕНЫ на два уведомления с разным `kind`, и это
    не косметика: дедуп шины держит 25 часов по ключу (subject, kind). Общий kind
    означал бы, что «прокси починился» в течение суток после «прокси сломался»
    молча подавлено — худший из возможных исходов, потому что владелец остаётся
    с ложным убеждением, что проблема жива.

    Восстановление — FYI: следующего шага у него нет, а §4 требует, чтобы
    ACTION_REQUIRED значил «есть что сделать». Оно уходит строкой в Daily Brief.

    what_if_ignored=None: цена простоя прокси зависит от того, шли ли в этот
    момент ордера, — фактической цифры на руках нет, а §3 запрещает выдумывать.
    """
    failures = [text for text, failing in alerts if failing]
    recoveries = [text for text, failing in alerts if not failing]

    out: list[Notification] = []

    if failures:
        out.append(
            Notification(
                severity=Severity.ACTION_REQUIRED,
                subject="infra:proxy",
                kind="proxy_fail",
                headline="Прокси для ордеров работает неправильно",
                what_happened=[
                    *failures,
                    "Через этот прокси уходят только заявки на сделки. "
                    "Пока он сломан, ордера будут отвергаться, а чтение данных продолжит работать.",
                ],
                what_if_ignored=None,
                what_to_do="Обнови сессию на iproyal.com/dashboard и проверь, что данные доступа в .env совпадают с ней.",
                actions=[],
                evidence=failures,
            )
        )

    if recoveries:
        out.append(
            Notification(
                severity=Severity.FYI,
                subject="infra:proxy",
                kind="proxy_recovery",
                headline="Прокси снова в порядке",
                what_happened=recoveries,
                what_if_ignored=None,
                what_to_do=None,
                actions=[],
                evidence=recoveries,
            )
        )

    return out


async def _send_alerts(alerts: list[tuple[str, bool]]) -> None:
    """Отправить edge-переходы через шину уведомлений."""
    try:
        await publish(to_notifications(alerts), actor="proxy_health_check")
    except Exception as e:
        logger.error("proxy_health_notify_failed", error=str(e))
