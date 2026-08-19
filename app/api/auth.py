"""
Web Auth API — вход через Telegram Login Widget для браузерного доступа.

Эндпоинты:
  POST /api/auth/callback  — получает данные виджета, ставит httpOnly cookie
  GET  /api/auth/me        — возвращает user info по cookie
  POST /api/auth/logout    — удаляет cookie
"""

import structlog
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.rate_limiter import limiter
from app.core.web_auth import decode_jwt_cookie, make_jwt_cookie, verify_telegram_widget_data

logger = structlog.get_logger()
router = APIRouter()

COOKIE_NAME = "oracle_web_auth"
COOKIE_MAX_AGE = 30 * 86400  # 30 дней


class TelegramWidgetData(BaseModel):
    id: str
    first_name: str
    username: str = ""
    photo_url: str = ""
    auth_date: str
    hash: str


@router.post("/callback")
@limiter.limit("10/minute")
async def telegram_callback(request: Request, data: TelegramWidgetData, response: Response):
    """
    Верифицирует данные Telegram Login Widget.
    Если telegram_id в ADMIN_TELEGRAM_IDS — ставит httpOnly auth cookie.

    Rate limit: 10/min per IP — защита от brute-force HMAC widget / admin telegram_id.
    """
    settings = get_settings()

    user = verify_telegram_widget_data(
        widget_data=data.model_dump(),
        bot_token=settings.telegram_bot_token,
    )
    if not user:
        raise HTTPException(status_code=401, detail="Invalid Telegram auth data")

    telegram_id = user["id"]
    if settings.admin_telegram_ids and telegram_id not in settings.admin_telegram_ids:
        logger.warning("web_auth_non_admin", telegram_id=telegram_id)
        raise HTTPException(status_code=403, detail="Access denied")

    if not settings.jwt_secret_key:
        raise HTTPException(status_code=500, detail="JWT secret not configured")

    token = make_jwt_cookie(telegram_id=telegram_id, secret=settings.jwt_secret_key)

    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )

    logger.info("web_auth_success", telegram_id=telegram_id, username=user.get("username"))
    return {"ok": True, "telegram_id": telegram_id, "username": user.get("username", "")}


@router.get("/me")
async def get_me(request: Request):
    """Возвращает telegram_id из cookie. 401 если нет/просрочен."""
    settings = get_settings()
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    if not settings.jwt_secret_key:
        raise HTTPException(status_code=500, detail="JWT secret not configured")

    telegram_id = decode_jwt_cookie(token=token, secret=settings.jwt_secret_key)
    if not telegram_id:
        raise HTTPException(status_code=401, detail="Session expired or invalid")

    is_admin = telegram_id in settings.admin_telegram_ids
    return {"telegram_id": telegram_id, "is_admin": is_admin}


@router.post("/logout")
@limiter.limit("20/minute")
async def logout(request: Request, response: Response):
    """Удаляет auth cookie."""
    response.delete_cookie(key=COOKIE_NAME, path="/")
    return {"ok": True}
