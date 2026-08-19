from __future__ import annotations

import json
import re


def parse_json_from_llm_response(content: str) -> dict:
    """Парсит JSON из ответа LLM, обрабатывая markdown-обёртки (```json ```)."""
    content = content.strip()
    fence_match = re.search(r"^```(?:json)?\s*([\s\S]*?)\s*```", content, re.MULTILINE)
    if fence_match:
        content = fence_match.group(1).strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM JSON parse error: {e}\nContent: {content[:200]}") from e
