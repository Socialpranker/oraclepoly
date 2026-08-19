"""IPS — инвестиционная политика владельца (Phase 2 C2).

Строгий парсер owner_ips.local.md (формат key: value, не проза) → IpsPolicy
+ детерминированный version hash (в БД хранится только hash — спека §9.1).
Спека: docs/superpowers/specs/2026-07-23-portfolio-agent-phase2-policy-engine-design.md §2 п.8;
концепт docs/specs/portfolio_analyst_agent.md §9.1. Изменить политику может
только владелец новой версией файла — hash меняется автоматически.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

_REQUIRED_KEYS = frozenset(
    {
        "capital_usd",
        "max_drawdown_pct",
        "liquidity_need_usd",
        "weekly_time_budget_hours",
        "forbidden_categories",
        "forbidden_platforms",
    }
)
_NUMERIC_KEYS = frozenset({"capital_usd", "max_drawdown_pct", "liquidity_need_usd", "weekly_time_budget_hours"})


class IpsParseError(ValueError):
    """Файл IPS не парсится — точная причина в message (гейт при этом неактивен + 🟡-маркер)."""


@dataclass(frozen=True)
class IpsPolicy:
    capital_usd: float
    max_drawdown_pct: float  # отрицательная доля (-1 < x < 0), напр. -0.20
    liquidity_need_usd: float
    weekly_time_budget_hours: float
    forbidden_categories: frozenset[str]  # lowercase; пустой = none
    forbidden_platforms: frozenset[str]
    version_hash: str  # sha256(канонич. строк)[:16] — паттерн proposal_id()


def _effective_lines(text: str) -> list[str]:
    """Значимые строки: без комментариев и пустых, stripped. Основа и парсинга, и hash."""
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def ips_version_hash(text: str) -> str:
    raw = "\n".join(_effective_lines(text))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _parse_list(value: str) -> frozenset[str]:
    if value.strip().lower() == "none":
        return frozenset()
    return frozenset(part.strip().lower() for part in value.split(",") if part.strip())


def parse_ips(text: str) -> IpsPolicy:
    """Строгий формат: каждая значимая строка = `key: value`. Иначе IpsParseError."""
    values: dict[str, str] = {}
    for line in _effective_lines(text):
        if ":" not in line:
            raise IpsParseError(f"invalid line (ожидается 'key: value'): {line!r}")
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key not in _REQUIRED_KEYS:
            raise IpsParseError(f"unknown key: {key!r}")
        if key in values:
            raise IpsParseError(f"duplicate key: {key!r}")
        if "<fill>" in value:
            raise IpsParseError(f"шаблон не заполнен: {key}")
        values[key] = value

    missing = _REQUIRED_KEYS - values.keys()
    if missing:
        raise IpsParseError(f"missing required keys: {sorted(missing)} (первый: {sorted(missing)[0]})")

    numbers: dict[str, float] = {}
    for key in _NUMERIC_KEYS:
        try:
            numbers[key] = float(values[key])
        except ValueError as exc:
            raise IpsParseError(f"not a number: {key}={values[key]!r}") from exc

    dd = numbers["max_drawdown_pct"]
    if not (-1.0 < dd < 0.0):
        raise IpsParseError(f"max_drawdown_pct must be a negative fraction in (-1, 0), got {dd}")

    return IpsPolicy(
        capital_usd=numbers["capital_usd"],
        max_drawdown_pct=dd,
        liquidity_need_usd=numbers["liquidity_need_usd"],
        weekly_time_budget_hours=numbers["weekly_time_budget_hours"],
        forbidden_categories=_parse_list(values["forbidden_categories"]),
        forbidden_platforms=_parse_list(values["forbidden_platforms"]),
        version_hash=ips_version_hash(text),
    )


def load_ips_file(path: Path) -> IpsPolicy | None:
    """None — файла нет (гейт неактивен, nag в брифе); IpsParseError пробрасывается (🟡-маркер)."""
    if not path.exists():
        return None
    return parse_ips(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class IpsGate:
    """Cycle-level IPS-гейт (спека §2 п.8): violated=True блокирует risk-вердикты как DQ RED."""

    active: bool
    violated: bool
    violations: tuple[str, ...]
    version_hash: str | None


INACTIVE_IPS_GATE = IpsGate(active=False, violated=False, violations=(), version_hash=None)


def evaluate_ips_gate(
    policy: IpsPolicy,
    forbidden_live_exposure: dict[str, float],
    portfolio_roi: float | None,
) -> IpsGate:
    """Нарушение v1 (решение владельца 2026-07-23): live-экспозиция в forbidden-категории
    ИЛИ портфельный bankroll-weighted ROI строго ниже max_drawdown_pct (strict <, паттерн C1).

    forbidden_platforms парсится и хранится, но в v1 НЕ проверяется (enforcement-скоуп
    v1 = категории + drawdown; площадки — задел под v2)."""
    violations: list[str] = []
    for category, stake in sorted(forbidden_live_exposure.items()):
        if category in policy.forbidden_categories and stake > 0:
            violations.append(f"forbidden category exposure: {category} (${stake:.2f})")
    if portfolio_roi is not None and portfolio_roi < policy.max_drawdown_pct:
        violations.append(
            f"portfolio drawdown: net ROI {portfolio_roi:.1%} below IPS limit {policy.max_drawdown_pct:.1%}"
        )
    return IpsGate(
        active=True,
        violated=bool(violations),
        violations=tuple(violations),
        version_hash=policy.version_hash,
    )
