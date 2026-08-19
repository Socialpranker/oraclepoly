"""Portfolio Analyst Agent — owner's report (Phase 0, monthly).

Спека: docs/specs/portfolio_analyst_agent.md §8 — "первый owner's report
(данные Phase 0 + ручной cost ledger)". Как и agent_daily_brief.py, это НЕ
LLM-синтезатор — чисто детерминированная агрегация. Task 10 брифа Step 3:

  - Агрегирует все agent_runs с момента запуска Phase 0 (kind='audit' —
    шесть replay-кейсов Task 4-9; kind='dq' — Task 8 sweep методологического
    долга; kind='experiment'/'proposal' пусты в Phase 0).
  - Список активных/протухших/contradicted клеймов (agent_claims.status).
  - Ручной cost ledger — Phase 0 хардкод: agent_llm_calls пустая таблица
    (LLM-синтезатор — Phase 1+, см. "Вне скоупа Phase 0" в implementation-плане),
    честно показываем "$0 LLM, инфраструктура на существующем VPS-хосте"
    (маргинальная стоимость ≈$0 — cron-джобы лёгкие, обоснование §7.1 спеки:
    "прокси не касается агента, новые read-only роль+4 таблицы — на
    существующем Postgres без доп. инфраструктуры").

Формат вывода: markdown-файл в research/agent-owners-report/ (research/ —
tracked, не удаляется, см. CLAUDE.md "research/ — tracked deep-research
отчёты") + тот же текст отчёта целиком в TG (send_admin_message, без кнопок —
TG-кнопочная механика agent_tg_communication.md вне скоупа Phase 0). Владелец
читает отчёт прямо в чате; файл на диске остаётся source of truth и путём
для внешних ссылок. _markdown_to_telegram_html() конвертирует markdown-разметку
(##, **bold**, `code`) в HTML, т.к. send_telegram_message шлёт с parse_mode=HTML.
send_admin_message сама режет текст на несколько сообщений при превышении
лимита Telegram (4096 символов) — отчёт с ростом числа клеймов почти всегда
его превышает.

DATABASE_URL vs agent_database_url: тот же выбор, что в agent_daily_brief.py
(см. его docstring) — обычный DATABASE_URL, это встроенный production cron-джоб,
не разовый read-only research-скрипт.

Cron: monthly, 1-го числа (Task 10 брифа Step 6). Первый прогон Phase 0
acceptance — ad-hoc вне расписания (Step 7 брифа, ВНЕ СКОУПА этой сессии —
прод недоступен, см. task-10-report.md).
"""

from __future__ import annotations

import html
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from app.services.database import fetch

logger = structlog.get_logger(__name__)

# Phase 0 инфраструктура — хардкод cost ledger (см. docstring). Обновится
# автоматикой из agent_llm_calls, когда заработает LLM-синтезатор (Phase 1+).
_PHASE0_LLM_COST_USD = 0.0
_PHASE0_INFRA_NOTE = (
    "Инфраструктура на существующем VPS-хосте — маргинальный прирост "
    "≈$0 (cron-джобы лёгкие, ML-инференс не задействован, read-only роль + "
    "4 таблицы на существующем Postgres). Прокси не касается агента (read-only, "
    "не размещает ордера)."
)

_ALL_RUNS_SQL = """
SELECT id, kind, name, verdict, config_hash, dataset_hash, started_at, finished_at
FROM agent_runs
ORDER BY started_at ASC
"""

_ALL_CLAIMS_SQL = """
SELECT id, statement, scope, status, last_verified_at, verify_halflife_days
FROM agent_claims
ORDER BY status, last_verified_at ASC
"""

_LLM_COST_SQL = """
SELECT COUNT(*) AS n_calls, COALESCE(SUM(cost_usd), 0)::float AS total_cost_usd
FROM agent_llm_calls
"""

_OUTPUT_DIR = Path("research/agent-owners-report")


def _build_report(
    runs: list[dict[str, Any]],
    claims: list[dict[str, Any]],
    llm_calls: dict[str, Any],
    now: datetime,
) -> str:
    """Pure formatter — без I/O, тестируемый отдельно.

    Каждое число несёт source (agent_runs.id/agent_claims.id) — §8 acceptance.
    """
    lines = [
        f"# Portfolio Analyst Agent — Owner's Report ({now.strftime('%Y-%m-%d')})",
        "",
        "Phase 0 (retrospective replay). Данные с момента запуска Phase 0 "
        "(миграция 2026-07-20) до момента генерации отчёта.",
        "",
        "## Прогоны (agent_runs)",
        "",
    ]

    if not runs:
        lines.append("Нет прогонов — Phase 0 ещё не исполнена ни одним кейсом.")
    else:
        by_kind: dict[str, list[dict[str, Any]]] = {}
        for r in runs:
            by_kind.setdefault(r["kind"], []).append(r)
        for kind in sorted(by_kind):
            kind_runs = by_kind[kind]
            lines.append(f"### {kind} ({len(kind_runs)})")
            lines.append("")
            for r in kind_runs:
                verdict_short = (r["verdict"] or "—").replace("\n", " ")[:200]
                lines.append(
                    f"- **{r['name']}** (agent_runs.id={r['id']}, "
                    f"config_hash={r['config_hash'][:12]}…, dataset_hash={r['dataset_hash'][:12]}…): "
                    f"{verdict_short}"
                )
            lines.append("")

    lines.extend(["## Клеймы (agent_claims)", ""])
    by_status: dict[str, list[dict[str, Any]]] = {}
    for c in claims:
        by_status.setdefault(c["status"], []).append(c)

    if not claims:
        lines.append("Реестр клеймов пуст.")
    else:
        for status in ("active", "expired", "contradicted", "superseded"):
            status_claims = by_status.get(status, [])
            if not status_claims:
                continue
            lines.append(f"### {status} ({len(status_claims)})")
            lines.append("")
            for c in status_claims:
                verified = c["last_verified_at"].strftime("%Y-%m-%d") if c["last_verified_at"] else "—"
                lines.append(
                    f"- {c['statement'][:150]} (scope={c['scope']}, "
                    f"agent_claims.id={c['id']}, last_verified={verified})"
                )
            lines.append("")

    n_active = len(by_status.get("active", []))
    n_expired = len(by_status.get("expired", []))
    n_contradicted = len(by_status.get("contradicted", []))

    lines.extend(
        [
            "## Cost ledger",
            "",
            f"- LLM: **${llm_calls['total_cost_usd']:.2f}** за {llm_calls['n_calls']} вызовов "
            "(agent_llm_calls — пусто в Phase 0, LLM-синтезатор не активирован, Phase 1+)."
            if llm_calls["n_calls"] > 0
            else f"- LLM: **${_PHASE0_LLM_COST_USD:.2f}** — LLM-слой ещё не активирован "
            "(agent_llm_calls пуста, честно, не пересказ).",
            f"- Инфраструктура: {_PHASE0_INFRA_NOTE}",
            "",
            "## Итого",
            "",
            f"- Прогонов: {len(runs)}",
            f"- Клеймов: {len(claims)} (active={n_active}, expired={n_expired}, contradicted={n_contradicted})",
            f"- Cost: ${llm_calls['total_cost_usd']:.2f} LLM + ~$0 инфраструктура",
        ]
    )

    return "\n".join(lines)


def _markdown_to_telegram_html(md: str) -> str:
    """Конвертировать markdown отчёта в HTML, понятный Telegram (parse_mode=HTML).

    Telegram HTML не поддерживает markdown-заголовки/bold — не конвертировать
    их значило бы слать '**bold**'/'## Заголовок' в чат буквально со звёздочками
    и решётками. Порядок важен: html.escape() ДО оборачивания в теги — иначе
    escape() экранирует и наши собственные <b>/<code>.
    """
    escaped = html.escape(md, quote=False)
    # ## Заголовок / ### Заголовок → жирная строка (Telegram не умеет в <h*>)
    escaped = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", escaped, flags=re.MULTILINE)
    # **bold** → <b>bold</b>
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    # `code` → <code>code</code>
    escaped = re.sub(r"`([^`]+?)`", r"<code>\1</code>", escaped)
    return escaped


async def run_agent_owners_report() -> dict[str, Any]:
    """Собрать owner's report и записать markdown-файл + короткое TG-уведомление."""
    runs = [dict(r) for r in await fetch(_ALL_RUNS_SQL)]
    claims = [dict(r) for r in await fetch(_ALL_CLAIMS_SQL)]
    llm_row = await fetch(_LLM_COST_SQL)
    llm_calls = dict(llm_row[0]) if llm_row else {"n_calls": 0, "total_cost_usd": 0.0}

    now = datetime.now(UTC)
    report_md = _build_report(runs, claims, llm_calls, now)

    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = _OUTPUT_DIR / f"{now.strftime('%Y-%m-%d')}.md"
    report_path.write_text(report_md, encoding="utf-8")

    from app.tasks.notifications import send_admin_message

    header = f"📈 <b>Owner's Report готов</b>\nФайл: <code>{report_path}</code>\n\n"
    await send_admin_message(header + _markdown_to_telegram_html(report_md))

    logger.info(
        "agent_owners_report_written",
        report_path=str(report_path),
        n_runs=len(runs),
        n_claims=len(claims),
    )
    return {
        "report_path": str(report_path),
        "n_runs": len(runs),
        "n_claims": len(claims),
        "llm_cost_usd": llm_calls["total_cost_usd"],
    }


logging.basicConfig(level=logging.INFO)
