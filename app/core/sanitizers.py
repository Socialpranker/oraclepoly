"""
Санитизация текста перед отправкой в LLM.

Удаляет prompt injection паттерны и control characters из внешних данных
(новости, пользовательский ввод) до передачи в OpenAI.
"""

from __future__ import annotations

import re
import unicodedata

# --- Unicode нормализация ---
# Таблица замен homoglyph-символов (кириллица и другие lookalikes → ASCII)
_HOMOGLYPH_MAP = str.maketrans(
    {
        "\u0430": "a",  # а → a
        "\u0435": "e",  # е → e
        "\u043e": "o",  # о → o
        "\u0440": "p",  # р → p
        "\u0441": "c",  # с → c
        "\u0443": "y",  # у → y
        "\u0445": "x",  # х → x
        "\u0456": "i",  # і → i (укр)
        "\u0410": "A",  # А → A
        "\u0412": "B",  # В → B
        "\u0415": "E",  # Е → E
        "\u041a": "K",  # К → K
        "\u041c": "M",  # М → M
        "\u041d": "H",  # Н → H
        "\u041e": "O",  # О → O
        "\u0420": "P",  # Р → P
        "\u0421": "C",  # С → C
        "\u0422": "T",  # Т → T
        "\u0425": "X",  # Х → X
        "\u2010": "-",  # ‐ (hyphen)
        "\u2011": "-",  # ‑ (non-breaking hyphen)
        "\u2012": "-",  # ‒ (figure dash)
        "\u2013": "-",  # – (en dash)
        "\u2014": "-",  # — (em dash)
        "\u2018": "'",  # '
        "\u2019": "'",  # '
        "\u201c": '"',  # "
        "\u201d": '"',  # "
        "\u00a0": " ",  # non-breaking space
        "\u200b": "",  # zero-width space
        "\u200c": "",  # zero-width non-joiner
        "\u200d": "",  # zero-width joiner
        "\ufeff": "",  # BOM
    }
)


def _normalize_unicode(text: str) -> str:
    """Нормализация unicode: NFKC + замена homoglyphs."""
    text = unicodedata.normalize("NFKC", text)
    return text.translate(_HOMOGLYPH_MAP)


# --- Паттерны prompt injection ---

# Английские паттерны (case-insensitive)
_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    # Прямые команды на переопределение системного промпта
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions?|prompts?|rules?)", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+)?(previous|prior|above)\s+(instructions?|prompts?|rules?)", re.IGNORECASE),
    re.compile(r"forget\s+(all\s+)?(previous|prior|above)\s+(instructions?|prompts?|rules?)", re.IGNORECASE),
    # Попытки представиться системой
    re.compile(r"(you\s+are|act\s+as|pretend\s+to\s+be)\s+(now\s+)?(a\s+)?(new|different)\s+", re.IGNORECASE),
    re.compile(r"(system\s+message|system\s*:)", re.IGNORECASE),
    # Команды на вывод определённого текста
    re.compile(r"(output\s+only|respond\s+with\s+only|say\s+exactly)\s+", re.IGNORECASE),
    # Блочные инъекции в квадратных скобках
    re.compile(r"\[.*?(?:IGNORE|INSTRUCTION|SYSTEM|OVERRIDE|INJECT).*?\]", re.IGNORECASE),
    # XML/HTML-подобные инъекции
    re.compile(r"<\s*/?(?:system|instruction|prompt|override|assistant|user|human|role)\s*/?\s*>", re.IGNORECASE),
    # Маркеры разделителей, имитирующие границы промпта
    re.compile(r"-{3,}\s*(?:system|instruction|prompt|end)\s*-{3,}", re.IGNORECASE),
    re.compile(r"#{3,}\s*(?:system|instruction|prompt)\s*#{3,}", re.IGNORECASE),
    # Новые паттерны: role hijacking
    re.compile(r"(new\s+instruction|updated?\s+instruction|override\s+instruction)", re.IGNORECASE),
    re.compile(
        r"(reveal|show|print|display|output)\s+(your\s+)?(system\s+)?(prompt|instructions?|rules?)", re.IGNORECASE
    ),
    re.compile(r"(do\s+not\s+follow|stop\s+following)\s+(the\s+)?(above|previous|prior|system)", re.IGNORECASE),
    # Markdown/formatting injection
    re.compile(r"```\s*(?:system|instruction|prompt)", re.IGNORECASE),
    # Encoded payloads: явные маркеры base64/hex декодирования
    re.compile(r"(decode|decrypt|base64|eval)\s*\(", re.IGNORECASE),
]

# Мультиязычные паттерны (французский, испанский, немецкий, русский, китайский, португальский)
_MULTILANG_PATTERNS: list[re.Pattern[str]] = [
    # Французский
    re.compile(r"ignor(?:ez|er)\s+(toutes?\s+)?les?\s+(instructions?|consignes?|r[èe]gles?)", re.IGNORECASE),
    re.compile(r"oubli(?:ez|er)\s+(toutes?\s+)?les?\s+instructions?", re.IGNORECASE),
    # Испанский
    re.compile(r"ignor(?:a|ar|e)\s+(todas?\s+)?las?\s+(instrucciones?|reglas?|indicaciones?)", re.IGNORECASE),
    re.compile(r"olvid(?:a|ar|e)\s+(todas?\s+)?las?\s+instrucciones?", re.IGNORECASE),
    # Немецкий
    re.compile(r"ignorier(?:e|en)\s+(alle\s+)?(vorherigen?\s+)?(Anweisungen?|Regeln?|Instruktionen?)", re.IGNORECASE),
    re.compile(r"vergiss\s+(alle\s+)?(vorherigen?\s+)?Anweisungen?", re.IGNORECASE),
    # Русский
    re.compile(r"игнорируй\s+(все\s+)?(предыдущие\s+)?(инструкции|правила|указания)", re.IGNORECASE),
    re.compile(r"забудь\s+(все\s+)?(предыдущие\s+)?(инструкции|правила|указания)", re.IGNORECASE),
    # Португальский
    re.compile(r"ignor(?:e|ar)\s+(todas?\s+)?as?\s+(instru[çc][õo]es?|regras?)", re.IGNORECASE),
    # Китайский (упрощённый)
    re.compile(r"忽略.{0,4}(?:之前|以前|上面|先前).{0,4}(?:指令|指示|规则|说明)"),
    re.compile(r"无视.{0,4}(?:之前|以前|上面|先前).{0,4}(?:指令|指示|规则|说明)"),
]

_REPLACEMENT = "[filtered]"


def sanitize_text(text: str, max_length: int = 2000) -> str:
    """Санитизация внешнего текста перед отправкой в LLM.

    1. Нормализует unicode (NFKC + homoglyph → ASCII)
    2. Удаляет control characters (кроме \\n, \\t)
    3. Заменяет prompt injection паттерны на [filtered] (EN + мультиязычные)
    4. Обрезает по max_length

    Args:
        text: Входной текст (новость, пользовательский ввод)
        max_length: Максимальная длина результата

    Returns:
        Очищенный текст
    """
    if not text:
        return ""

    # 1. Удаляем control characters (оставляем \n и \t)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", text)

    # 2. Мультиязычные injection паттерны ДО homoglyph-нормализации
    #    (иначе кириллица/CJK будет повреждена)
    for pattern in _MULTILANG_PATTERNS:
        text = pattern.sub(_REPLACEMENT, text)

    # 3. Unicode нормализация (NFKC + homoglyphs → ASCII для обхода через lookalikes)
    text = _normalize_unicode(text)

    # 4. Английские injection паттерны ПОСЛЕ нормализации
    #    (ловит homoglyph-обход: ignоre → ignore)
    for pattern in _INJECTION_PATTERNS:
        text = pattern.sub(_REPLACEMENT, text)

    # 5. Обрезаем
    return text.strip()[:max_length]
