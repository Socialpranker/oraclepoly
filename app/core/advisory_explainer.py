"""Advisory-explainer через OpenRouter (Phase 2 C3b §3.3).

Синхронный разбор advisory-вердикта для кнопки Request review. Изолирован от
get_llm_client (отдельный клиент под explainer, решение владельца 2026-07-24).
Graceful: нет ключа / LLM упал → None. Вход к LLM — только поля одной
proposal-строки (минимум прод-данных). LLM НЕ решает вердикты — только объясняет.
"""

from __future__ import annotations

import json

import structlog
from openai import AsyncOpenAI

from app.core.config import get_settings

logger = structlog.get_logger()

_SYSTEM = (
    "Ты — ассистент трейдера на prediction-маркетах. Кратко (2-4 предложения, "
    "по-русски) объясни, что означает этот advisory-вердикт policy engine и на "
    "что человеку смотреть. Не выдумывай цифры и денежный эффект. Не принимай "
    "решение за человека — только поясняй."
)


async def explain_advisory(proposal_row: dict) -> str | None:
    """Человекочитаемый разбор advisory. None → explainer недоступен (выключен флагом / нет ключа / LLM упал)."""
    settings = get_settings()
    if not settings.advisory_explainer_enabled:
        return None  # рубильник: фича выключена — наружу не ходим даже при наличии ключа
    if not settings.openrouter_api_key:
        return None

    context = json.dumps(
        {
            "verdict": proposal_row.get("verdict"),
            "scope": proposal_row.get("scope"),
            "reason": proposal_row.get("reason"),
            "metrics": proposal_row.get("metrics"),
        },
        ensure_ascii=False,
    )
    try:
        client = AsyncOpenAI(api_key=settings.openrouter_api_key, base_url="https://openrouter.ai/api/v1")
        resp = await client.chat.completions.create(
            model=settings.advisory_explainer_model,
            max_tokens=settings.advisory_explainer_max_tokens,
            messages=[{"role": "system", "content": _SYSTEM}, {"role": "user", "content": context}],
        )
        return resp.choices[0].message.content
    except Exception:
        logger.warning("advisory_explainer_failed", exc_info=True)
        return None
