from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI

from app.core.config import get_settings


def get_llm_client(
    temperature: float | None = None,
    model: str | None = None,
    timeout: int = 30,
    max_retries: int = 2,
    json_mode: bool = False,
) -> ChatOpenAI:
    """Единая фабрика LLM-клиента для всех агентов.

    Поддерживаемые провайдеры:
    - openai (default): OpenAI API, ключ openai_api_key
    - cloudflare: Cloudflare Workers AI (OpenAI-совместимый API),
      ключ cf_api_token, base_url из cf_account_id.
      Если cf_account_id пустой — fallback на openai.

    Args:
        json_mode: Если True — включает response_format={"type": "json_object"},
                   гарантируя валидный JSON от модели.
    """
    settings = get_settings()

    use_cloudflare = settings.llm_provider == "cloudflare" and bool(settings.cf_account_id)

    if use_cloudflare:
        resolved_model = model or settings.cf_model_chat
        resolved_api_key = settings.cf_api_token
        resolved_base_url: str | None = f"https://api.cloudflare.com/client/v4/accounts/{settings.cf_account_id}/ai/v1"
    else:
        resolved_model = model or settings.llm_analysis_model
        resolved_api_key = settings.openai_api_key
        resolved_base_url = settings.llm_base_url

    kwargs: dict[str, Any] = {
        "model": resolved_model,
        "temperature": temperature if temperature is not None else settings.llm_temperature,
        "max_tokens": settings.llm_max_tokens,
        "api_key": resolved_api_key,
        "timeout": timeout,
        "max_retries": max_retries,
    }
    if resolved_base_url:
        kwargs["base_url"] = resolved_base_url
    if json_mode and not use_cloudflare:
        # CF Workers AI не поддерживает json_object, промпты уже требуют JSON
        kwargs["model_kwargs"] = {"response_format": {"type": "json_object"}}
    return ChatOpenAI(**kwargs)
