"""
Oracle Backend - FastAPI Entry Point
"""

import uvloop

uvloop.install()

import os
from contextlib import asynccontextmanager

import sentry_sdk
import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import auth, bot, calibration, d45, telegram, tier2
from app.core.cache import close_redis, init_redis
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.core.rate_limiter import setup_rate_limiting
from app.services.database import close_db, init_db

logger = structlog.get_logger()

# --- Sentry ---
_settings = get_settings()
if isinstance(_settings.sentry_dsn, str) and _settings.sentry_dsn.startswith("http"):
    sentry_sdk.init(
        dsn=_settings.sentry_dsn,
        traces_sample_rate=0.2,
        profiles_sample_rate=0.1,
        environment=os.getenv("SENTRY_ENVIRONMENT", "production"),
        send_default_pii=False,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    setup_logging()
    settings = get_settings()
    from app.services.database import _mask_dsn

    logger.info(
        "backend_starting",
        database_host=_mask_dsn(settings.database_url),
        r2_bucket=settings.r2_bucket_name,
    )

    # Phase 1 Stage 7: withdrawal flow fail-stop. WITHDRAWAL_ENABLED=true без
    # destination address = misconfiguration → отказываем старт. Иначе propose
    # cron создаст proposed row с пустым адресом и approver получит сюрприз.
    if settings.withdrawal_enabled and not settings.withdrawal_destination_address:
        raise RuntimeError("WITHDRAWAL_ENABLED=true но WITHDRAWAL_DESTINATION_ADDRESS пуст — fail-stop")

    await init_db()
    await init_redis(settings.redis_url)

    # Cold-start guard: если markets таблица пустая (fresh install), запустить
    # markets_sync ДО старта WS — иначе WS подключится с empty subscriptions,
    # начнёт reconnect-loop каждые 50s (Polymarket idle timeout).
    from app.services.database import fetchval

    market_count = await fetchval("SELECT COUNT(*) FROM markets WHERE is_active = true")
    if not market_count:
        logger.info("cold_start_running_initial_market_sync")
        try:
            from app.jobs.market_sync import run_market_sync

            result = await run_market_sync()
            logger.info(
                "cold_start_market_sync_complete",
                markets_synced=result.get("markets_synced", 0),
                events_synced=result.get("events_synced", 0),
                errors=result.get("errors", 0),
            )
        except Exception as exc:
            logger.error("cold_start_market_sync_failed", error=str(exc))

    # Запуск WebSocket сервиса для real-time market microstructure
    from app.services.polymarket_ws_service import get_ws_service

    ws_service = get_ws_service()
    await ws_service.start()

    # Phase 1 Stage 5: heart-beat dead-man tasks.
    # heartbeat_writer пишет bot:risk_heartbeat (daemon его читает).
    # main_self_check читает bot:risk_daemon_alive (если daemon мёртв → SET kill).
    import asyncio as _asyncio

    from app.core.heartbeat import heartbeat_writer_loop, main_self_check_loop

    heartbeat_task = _asyncio.create_task(heartbeat_writer_loop())
    self_check_task = _asyncio.create_task(main_self_check_loop())

    # D45 Chainlink lead-lag (Shadow only): запускаем Pyth Hermes SSE +
    # Binance backup WS + 5-сек scan loop. Опционально через
    # d45_chainlink_leadlag_enabled.
    d45_pyth_stream = None
    d45_binance_stream = None
    d45_scan_task = None
    d45_stop_event = None
    if settings.d45_chainlink_leadlag_enabled:
        from app.core.d45_loop import d45_chainlink_leadlag_loop
        from app.services.binance_stream_service import get_binance_stream
        from app.services.pyth_stream_service import get_pyth_stream

        d45_pyth_stream = get_pyth_stream()
        d45_binance_stream = get_binance_stream()
        await d45_pyth_stream.start()
        await d45_binance_stream.start()
        d45_stop_event = _asyncio.Event()
        d45_scan_task = _asyncio.create_task(d45_chainlink_leadlag_loop(d45_stop_event))
        logger.info("d45_chainlink_leadlag_enabled")

    yield

    heartbeat_task.cancel()
    self_check_task.cancel()
    if d45_scan_task is not None and d45_stop_event is not None:
        d45_stop_event.set()
        d45_scan_task.cancel()
        try:
            await d45_scan_task
        except (_asyncio.CancelledError, Exception):
            pass
    if d45_pyth_stream is not None:
        await d45_pyth_stream.stop()
    if d45_binance_stream is not None:
        await d45_binance_stream.stop()
    # Close shared httpx client used by Pyth REST history fetcher.
    try:
        from app.services.pyth_history_service import close_client as _close_pyth_history

        await _close_pyth_history()
    except Exception:
        pass
    await ws_service.stop()
    await close_redis()
    await close_db()
    logger.info("backend_shutting_down")


_is_production = os.getenv("SENTRY_ENVIRONMENT", "production") == "production"

app = FastAPI(
    title="Oracle Backend",
    description="Predictive hub for Polymarket - Event-driven AI system",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None if _is_production else "/docs",
    redoc_url=None if _is_production else "/redoc",
    openapi_url=None if _is_production else "/openapi.json",
)

# CORS - configured via CORS_ORIGINS env variable
settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "x-telegram-init-data", "Authorization"],
)

# --- Routes (self-trading bot only) ---
app.include_router(bot.router, prefix="/api/bot", tags=["Bot"])
app.include_router(telegram.router, prefix="/api/telegram", tags=["Telegram"])
app.include_router(auth.router, prefix="/api/auth", tags=["Auth"])
app.include_router(tier2.router, prefix="/api/tier2", tags=["Tier2"])
app.include_router(calibration.router, prefix="/api/calibration", tags=["Calibration"])
app.include_router(d45.router, prefix="/api/d45", tags=["D45"])


# --- Security ---
setup_rate_limiting(app)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Обезличенный 422 — не раскрывает внутреннюю структуру моделей."""
    return JSONResponse(status_code=422, content={"detail": "Validation error"})


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Глобальный обработчик — не утекает stack trace и internal details."""
    logger.error("unhandled_exception", error=str(exc), path=str(request.url.path))
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok"}
