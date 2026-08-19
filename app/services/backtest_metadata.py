"""Parse canonical backtest markdown files (docs/backtests/*.md) into JSON.

Single source of truth for canonical backtest metadata.

Each markdown file:
- YAML frontmatter (between `---` lines) → metadata dict
- Markdown body → human-readable details

Used by:
- GET /v2/detectors/{detector}/backtest API endpoint
- GET /v2/detectors aggregates (backtest_predicted_daily_usd)

См. docs/detector_lifecycle.md for backtest stage definition.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# Repo root → docs/backtests/
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKTESTS_DIR = _REPO_ROOT / "docs" / "backtests"


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)", re.DOTALL)
_FIELD_RE = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_]*):\s*(.*)$")


def _coerce(value: str) -> Any:
    """Coerce YAML scalar value to Python type (no nested structures)."""
    v = value.strip()
    if not v:
        return None
    if v.lower() in ("true", "yes"):
        return True
    if v.lower() in ("false", "no"):
        return False
    if v.lower() in ("null", "~"):
        return None
    # Number
    try:
        if "." in v or "e" in v.lower():
            return float(v)
        return int(v)
    except ValueError:
        pass
    # Quoted string
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        return v[1:-1]
    return v


def _parse_simple_frontmatter(text: str) -> dict[str, Any]:
    """Minimal YAML-frontmatter parser — flat key:value pairs only.

    Sufficient for our backtest markdown frontmatter. Avoids PyYAML dependency.
    """
    out: dict[str, Any] = {}
    for line in text.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        m = _FIELD_RE.match(line)
        if not m:
            continue
        key, raw = m.group(1), m.group(2)
        out[key] = _coerce(raw)
    return out


def parse_backtest_md(detector: str) -> dict[str, Any] | None:
    """Read docs/backtests/{detector}.md, parse frontmatter + body.

    Returns None if file doesn't exist.
    """
    path = BACKTESTS_DIR / f"{detector}.md"
    if not path.exists():
        return None

    text = path.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {"error": "missing_frontmatter", "detector": detector}

    meta = _parse_simple_frontmatter(m.group(1))
    body = m.group(2).strip()
    return {**meta, "body_markdown": body}


def list_available_backtests() -> list[str]:
    """Detector names with available backtest files."""
    if not BACKTESTS_DIR.exists():
        return []
    return sorted(p.stem for p in BACKTESTS_DIR.glob("*.md"))


_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_TABLE_ROW_RE = re.compile(r"^\|(.+)\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\|[\s\-:|]+\|\s*$")


def _split_sections(body: str) -> dict[str, str]:
    """Разбить markdown body на секции по '## ' headings.

    Возвращает dict {heading_lower: section_text}. heading нормализуется в
    lowercase + spaces→underscore для удобной выборки.
    """
    sections: dict[str, str] = {}
    matches = list(_HEADING_RE.finditer(body))
    for i, m in enumerate(matches):
        heading = m.group(1).strip().lower()
        # Нормализация: "BUCKET_WR table (canonical 821 days, NO-only)" → "bucket_wr_table"
        key = heading.split("(")[0].strip().replace(" ", "_").replace("-", "_")
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sections[key] = body[start:end].strip()
    return sections


def _parse_md_table(text: str) -> list[dict[str, Any]]:
    """Распарсить markdown table в list[dict] с column headers как ключами.

    Поддерживает базовый формат `| col1 | col2 | ... |`. Игнорирует separator
    row `|---|---|`. Coerces числовые ячейки. Возвращает [] если table нет.
    """
    rows = [line for line in text.splitlines() if _TABLE_ROW_RE.match(line)]
    rows = [r for r in rows if not _TABLE_SEP_RE.match(r)]
    if len(rows) < 2:  # нужны хотя бы header + 1 data row
        return []
    headers = [c.strip() for c in rows[0].strip("|").split("|")]
    out = []
    for row in rows[1:]:
        cells = [c.strip() for c in row.strip("|").split("|")]
        if len(cells) != len(headers):
            continue
        record: dict[str, Any] = {}
        for h, c in zip(headers, cells):
            # Coerce numeric (strip %, $, +, ,)
            cleaned = c.replace("%", "").replace("$", "").replace("+", "").replace(",", "").strip()
            try:
                record[h] = float(cleaned) if "." in cleaned else int(cleaned)
            except ValueError:
                record[h] = c
        out.append(record)
    return out


def _parse_bullets(text: str) -> list[dict[str, str]]:
    """Распарсить bulleted list `- **Key:** value` в list[{key, value}]."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("-"):
            continue
        # `- **Side:** NO only` → {key: 'Side', value: 'NO only'}
        m = re.match(r"^-\s*\*\*([^:*]+):\*\*\s*(.+?)$", line)
        if m:
            out.append({"key": m.group(1).strip(), "value": m.group(2).strip()})
            continue
        # `- Side: NO only` (без bold)
        m = re.match(r"^-\s*([^:]+):\s*(.+?)$", line)
        if m:
            out.append({"key": m.group(1).strip(), "value": m.group(2).strip()})
    return out


def _first_paragraph(text: str) -> str:
    """Вернуть первый текстовый параграф (до пустой строки/heading/table/list).

    Распознаёт inline `**Bold:** text` как валидный текст (для конструкции
    `**Hypothesis:** ...` в начале backtest md). Игнорирует bullet lists `- `,
    code blocks ```, headings #, table rows |.
    """
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            if lines:
                break
            continue
        # Block-level markdown — пропускаем (или break если уже есть текст)
        if stripped.startswith(("#", "|", "```")) or re.match(r"^[-*]\s", stripped):
            if lines:
                break
            continue
        # Inline bold/emphasis — strip из вывода, но строка считается валидной
        clean = re.sub(r"\*\*([^*]+)\*\*", r"\1", stripped)
        clean = re.sub(r"\*([^*]+)\*", r"\1", clean).strip()
        if clean:
            lines.append(clean)
    return " ".join(lines).strip()


def parse_body_sections(body: str) -> dict[str, Any]:
    """Распарсить markdown body backtest md в структурированные секции для UI.

    Возвращает dict с типизированными секциями:
      hypothesis: str — первый параграф из body (или из ## Hypothesis секции)
      filters: list[{key, value}] — bulleted list из ## Filters
      bucket_wr_table: list[dict] — markdown table из BUCKET_WR/Per-anchor секций
      category_breakdown: list[dict] — markdown table из Per-category/Category секций
      promotion_criteria: dict — ключи shadow_to_virtual, virtual_to_live из ## Promotion criteria

    Если секция не найдена — ключ отсутствует или пустой list/dict.
    """
    sections = _split_sections(body)
    out: dict[str, Any] = {}

    # Hypothesis: первый параграф body (до первого heading) или ## Hypothesis
    if "hypothesis" in sections:
        out["hypothesis"] = _first_paragraph(sections["hypothesis"])
    else:
        # Берём первый параграф всего body (до первого ##)
        first_heading = _HEADING_RE.search(body)
        intro = body[: first_heading.start()] if first_heading else body
        out["hypothesis"] = _first_paragraph(intro)

    # Filters: bullets из ## Filters
    if "filters" in sections:
        out["filters"] = _parse_bullets(sections["filters"])

    # BUCKET_WR table: ищем по нескольким возможным заголовкам
    for key in ("bucket_wr_table", "per_anchor_metrics", "per_(side,_anchor_band)_wr"):
        if key in sections:
            table = _parse_md_table(sections[key])
            if table:
                out["bucket_wr_table"] = table
                break

    # Category breakdown
    for key in ("per_category_breakdown", "category_breakdown"):
        if key in sections:
            table = _parse_md_table(sections[key])
            if table:
                out["category_breakdown"] = table
                break

    # Promotion criteria
    if "promotion_criteria" in sections:
        text = sections["promotion_criteria"]
        criteria: dict[str, str] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("-"):
                continue
            m = re.match(r"^-\s*\*\*([^*]+)\*\*\s*(?:\(([^)]+)\))?:?\s*(.+?)$", line)
            if m:
                label = m.group(1).strip().lower()
                # "Shadow → Virtual" → "shadow_to_virtual"
                key = label.replace("→", "to").replace(" ", "_").replace("__", "_")
                if "to" in key:
                    criteria[key] = m.group(3).strip()
        if criteria:
            out["promotion_criteria"] = criteria

    return out
