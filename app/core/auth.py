"""
FastAPI authentication dependencies for Telegram initData validation.

Dependency types:
- require_telegram_user: 401 if missing, 403 if invalid
- optional_telegram_user: None if missing, 403 if invalid
- check_admin: Bearer admin_api_key OR Telegram admin user + audit logging
"""

import hmac

import structlog
from fastapi import Header, HTTPException, Request

from app.core.config import get_settings
from app.core.telegram import validate_telegram_init_data
from app.core.web_auth import decode_jwt_cookie

logger = structlog.get_logger()


async def require_telegram_user(
    x_telegram_init_data: str | None = Header(None),
) -> dict:
    """Require valid Telegram initData. Returns user dict."""
    if not x_telegram_init_data:
        raise HTTPException(status_code=401, detail="Missing Telegram init data")

    user_data = validate_telegram_init_data(x_telegram_init_data)
    if not user_data:
        raise HTTPException(status_code=403, detail="Invalid Telegram init data")

    if not user_data.get("id"):
        raise HTTPException(status_code=400, detail="User ID not found in Telegram data")

    return user_data


async def optional_telegram_user(
    x_telegram_init_data: str | None = Header(None),
) -> dict | None:
    """Optional Telegram initData. Returns user dict or None."""
    if not x_telegram_init_data:
        return None

    user_data = validate_telegram_init_data(x_telegram_init_data)
    if not user_data:
        raise HTTPException(status_code=403, detail="Invalid Telegram init data")

    if not user_data.get("id"):
        raise HTTPException(status_code=400, detail="User ID not found in Telegram data")

    return user_data


def check_admin(request: Request) -> dict:
    """Проверка admin-доступа: Bearer admin_api_key ИЛИ Telegram init-data ИЛИ JWT web-cookie.

    Поддерживает три метода:
    1. Authorization: Bearer <admin_api_key> — для cron/scripts
    2. x-telegram-init-data с telegram_id из admin_telegram_ids — для TG Mini App
    3. JWT cookie 'oracle_web_auth' (ставится в /api/auth/callback) — для web (example.com)

    Returns:
        dict с информацией об admin: {"method": "bearer"|"telegram"|"web_cookie", "admin_id": ...}
    """
    settings = get_settings()

    # Метод 1: Bearer token (для cron/scripts) — проверяем все активные ключи
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        for key in settings.active_admin_keys:
            if hmac.compare_digest(token, key):
                admin_info = {"method": "bearer", "admin_id": "api_key"}
                _audit_log(request, admin_info)
                return admin_info

    # Метод 2: Telegram admin user (для TG Mini App)
    init_data = request.headers.get("x-telegram-init-data", "")
    if init_data and settings.admin_telegram_ids:
        user_data = validate_telegram_init_data(init_data)
        if user_data and user_data.get("id") in settings.admin_telegram_ids:
            admin_info = {"method": "telegram", "admin_id": user_data["id"]}
            _audit_log(request, admin_info)
            return admin_info

    # Метод 3: JWT web-cookie (для web-логина через Telegram Login Widget)
    cookie_token = request.cookies.get("oracle_web_auth")
    if cookie_token and settings.jwt_secret_key and settings.admin_telegram_ids:
        telegram_id = decode_jwt_cookie(token=cookie_token, secret=settings.jwt_secret_key)
        if telegram_id and telegram_id in settings.admin_telegram_ids:
            admin_info = {"method": "web_cookie", "admin_id": telegram_id}
            _audit_log(request, admin_info)
            return admin_info

    raise HTTPException(status_code=403, detail="Forbidden")


def _audit_log(request: Request, admin_info: dict) -> None:
    """Логирование admin-действия в structlog."""
    logger.info(
        "admin_action",
        admin_id=admin_info["admin_id"],
        auth_method=admin_info["method"],
        path=str(request.url.path),
        method=request.method,
        client_ip=request.client.host if request.client else "unknown",
    )
