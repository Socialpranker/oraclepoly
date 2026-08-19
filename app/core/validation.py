"""
Централизованные regex-паттерны для валидации API-параметров.

Использование:
    from app.core.validation import MARKET_ID_PATTERN, SLUG_PATTERN, UUID_PATTERN
"""

from __future__ import annotations

import re

# UUID or slug-safe ID pattern (alphanumeric, hyphens, underscores), макс. 100 символов
MARKET_ID_PATTERN = re.compile(r"^[a-zA-Z0-9\-_]{1,100}$")

# Slug-safe pattern (alphanumeric, hyphens, underscores), макс. 200 символов
SLUG_PATTERN = re.compile(r"^[a-zA-Z0-9\-_]{1,200}$")

# Стандартный UUID v4 в формате xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
UUID_PATTERN = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
