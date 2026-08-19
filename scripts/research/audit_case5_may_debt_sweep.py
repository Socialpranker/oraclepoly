"""Phase 0 replay-кейс 5: sweep методологического долга мая (`research/` + `MEMORY.md`).

Портфель Analyst Agent, Phase 0 (docs/superpowers/plans/2026-07-20-portfolio-agent-phase0.md,
Task 8) — §3.1 спеки называет это «методологический долг мая»: клеймы/вердикты,
датированные до методологического скачка (look-ahead-safe дисциплина), и не
переверифицированные новой дисциплиной, должны получить карантинный статус в
`agent_claims` по умолчанию, а не молча считаться свежими.

Концептуально ДРУГОЙ кейс, чем replay-кейсы 1-4/6 (audit_case1/2/3/4/6): те
читают `detector_signals`/`execution_orders` и параметризованы по `as_of`.
Этот кейс читает ФАЙЛОВУЮ систему (`research/*.md`, `MEMORY.md`) — БД нужна
ТОЛЬКО для записи результата в `agent_claims`/`agent_runs` через
`agent_database_url`, не для чтения источников долга.

=== Step 1: дата-водораздел = 2026-06-13 (не ~2026-06-15 из §3.1) ===

`research/whale-cluster-eda-2026-06-13.md` — первый документированный честный
look-ahead-safe пересчёт с явной методологической рефлексией: наивный EDA дал
41 кластер с edge −0.28, look-ahead-safe пересчёт (кластер обязан
сформироваться ≥1 дня до резолва) схлопнул выборку до 4 кластеров ("N=4 =
статистически пусто", строка 5) и явно предписал: "Перед любым shadow:
переписать как look-ahead-safe" (строка 72). Это самая ранняя найденная в
`research/` документированная точка, где look-ahead-safe пересчёт стал
явной практикой, а не разовым чек-листом.

~2026-06-15 из §3.1 спеки НЕ подтверждена ни одним документом (интерполяция
между 06-13 и 06-24 — отклонена). 2026-06-24
(`research/backtest-prod-gap-postmortem-2026-06-24/README.md`,
memory `backtest-prod-gap-is-metric-not-optimism-2026-06-24.md`) — это
постмортем/аннулирование Wilson-vs-NetROI задним числом, следствие
дисциплины, не сама граница.

Отдельная оговорка (важно для Step 2 ниже): термин "control-first" как явно
названный паттерн задокументирован только в июльских документах
(`research/fable-research-map-2026-07-19/01_edge_and_detectors.md`,
`research/d52-whale-flow-backtest-2026-07-09/`) — §3.1 объединяет под одной
датой два разновременных явления: look-ahead-safe (с 06-13) и control-first
как названная практика (с июля). Признак (б) "отсутствие control-group" будет
истинным почти для всей истории `research/` вплоть до июля — это ОЖИДАЕМЫЙ
объём находок, не сигнал, что sweep сломан.

=== Step 2-3: курируемый реестр, не рантайм-эвристика ===

Инвентаризация долга — это распознавание паттерна в свободном тексте
(признаки (а) win-rate-only вердикт, (б) нет control/baseline, (в) нет
защиты от look-ahead), которое не сводится к надёжному regex/keyword-скану
без большого числа ложных срабатываний (напр. "control" в
`risk-engine-production-2026-05` означает risk-контроль/kill-switch, не
control-group; "Wilson" появляется и в честных пост-06-13 документах). Найдя
это на практике при чтении `research/` (2026-07-20, read-only), список ниже
курируется вручную по итогам прочтения каждого файла/директории —
воспроизводимость (Acceptance брифа) обеспечивается тем, что сам РЕЕСТР
находок детерминирован и версионирован в этом файле (Step 4 ниже даёт
один и тот же список при каждом прогоне, если `research/` не изменился),
а не тем, что скрипт заново парсит текст при каждом запуске. Чистая функция
`classify_debt_signals()` ниже — вспомогательный классификатор по ключевым
словам, используется только как self-check (см. tests/), не как источник
находок в DEBT_REGISTRY.

Директории research/, проверенные и НЕ давшие явных признаков долга (пусто
или уже дисциплинированы до водораздела) — не включены в реестр:
`shadow-gates-methodology-2026-05`, `virtual-feedback-2026-05`,
`new-detector-candidates-2026-06-09` (уже использует honest realized ROI net
и "no look-ahead" явно, см. report.md), `risk-engine-production-2026-05`
("control" = kill-switch, не control-group).

Недатированные директории `research/` (без даты в имени) — все проверены
через `git log --diff-filter=A` (дата первого коммита): все в диапазоне
2026-03-22..2026-04-16, задолго до водораздела. Восемь из брифовой оценки
"до 8 директорий без даты" — 8 директорий верхнего уровня
(copy-trading-prediction-markets-whale-signals,
oracle-edge-cluster-analysis, polymarket-accuracy-vs-polling-elections,
polymarket-execution-bot, polymarket-orthogonal-strategies,
prediction-market-edge-sources,
novye-torgovye-strategii-prediction-markets-polymarket) + весь
`research/archive/` (9 директорий, все первый коммит 2026-04-10) — итого
входят в carve-out ниже как "явно старые, не разбираются построчно" (все
старше водораздела на 2+ месяца, признаки долга презюмируются по возрасту,
не перечисляются индивидуально — Acceptance просит 5+ конкретных элементов
со ссылкой на файл, не полную опись всего каталога).

=== Step 3: MEMORY.md ===

Важная находка: ни один topic-файл в `~/.claude/.../memory/` не датирован
раньше 2026-06-24 (самый ранний — `backtest-prod-gap-is-metric-not-optimism-
2026-06-24.md`). Признаки долга в MEMORY.md-топиках как таковых искать не на
чем — вся текущая долгоживущая память уже пост-водораздел. Что реально есть
в MEMORY.md — ССЫЛКИ на майские research-документы внутри более новых
топик-файлов, из которых один прямо описывает уже сработавший долг: D44
"verified KEEPER" (оригинал `edge-synthesis-2026-05-24/README.md:58`,
аннулирован `backtest-prod-gap-is-metric-not-optimism-2026-06-24.md`) — это
и есть Acceptance-требуемый "уже закрытый прецедент", подтверждающий, что
sweep-метод в принципе находит то, что реально было.

Запускать ТОЛЬКО на проде для записи в БД (read-only agent-роль, см.
CLAUDE.md "scripts/research/*.py — one-off скрипты для VPS"). Чтение
`research/`/`MEMORY.md` — файловая система, доступна и локально; НЕ
проверено на проде (SSH к прод-БД для этого кейса не требуется по брифу,
см. Scope Adjustment task-8-brief.md):
    docker exec oraclepoly-backend python scripts/research/audit_case5_may_debt_sweep.py

До применения Task 1 (миграция agent_runs/agent_claims) и Task 2 (read-only
роль oraclepoly_agent_ro + AGENT_DATABASE_URL) на проде скрипт не подключится
к БД — init_db(dsn=settings.agent_database_url) упадёт. Ожидаемо, не баг
этого скрипта. Fail-loud guard на отсутствующий agent_database_url (см.
CROSS-TASK finding в .superpowers/sdd/progress.md — сиблинги audit_case2/3/6
имели silent-fallback баг, этот скрипт его НЕ повторяет).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

# Allow execution from repo root: PYTHONPATH=.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.core.config import get_settings  # noqa: E402
from app.services.database import close_db, execute, fetchrow, init_db  # noqa: E402

CUTOFF_DATE = date(2026, 6, 13)
CUTOFF_RATIONALE = (
    "research/whale-cluster-eda-2026-06-13.md — первый документированный честный "
    "look-ahead-safe пересчёт (наивный EDA 41 кластер edge -0.28 -> look-ahead-safe "
    "4 кластера, 'N=4 = статистически пусто'; явное предписание 'Перед любым shadow: "
    "переписать как look-ahead-safe'). НЕ ~2026-06-15 из спеки §3.1 (интерполяция, "
    "не подтверждена документом). НЕ 2026-06-24 (backtest-prod-gap-postmortem — "
    "следствие/аннулирование задним числом, не сама граница)."
)

DebtType = str  # "a" | "b" | "c" — win-rate-only | no-control-group | no-look-ahead-guard

_DEBT_LABELS: dict[DebtType, str] = {
    "a": "вердикт на основе Win Rate без Net ROI пересчёта",
    "b": "отсутствие явной control/baseline группы сравнения",
    "c": "отсутствие явной защиты от look-ahead (no time-aware split)",
}


@dataclass(frozen=True)
class DebtItem:
    """Один найденный элемент методологического долга (Step 2-3 брифа)."""

    source_path: str  # относительный путь от repo root
    doc_date: date  # дата документа (не сегодняшняя!) -> last_verified_at
    debt_types: tuple[DebtType, ...]
    statement: str  # свободный текст утверждения, идёт в agent_claims.statement
    scope: str  # "type:value" конвенция
    quarantine_status: str  # 'expired' | 'active'
    evidence_note: str  # что именно найдено (строка/цитата), для verdict
    already_closed_precedent: bool = False  # уже сработавший долг (D44, drill-matrix)
    line_ref: str | None = None
    always_include: bool = False  # игнорировать cutoff-фильтр (Step 3 meta-finding про MEMORY.md coverage)


# ─── Step 2-3: курируемый реестр находок (verified read-only, 2026-07-20) ────
#
# Каждая запись — результат ручного чтения файла (см. docstring выше почему
# не рантайм-эвристика). Порядок: research/ датированные файлы -> research/
# директории с суффиксом только месяца -> MEMORY.md-производные записи.
DEBT_REGISTRY: tuple[DebtItem, ...] = (
    # --- уже закрытый прецедент #1: D44 "verified KEEPER" -> annulled ---
    DebtItem(
        source_path="research/edge-synthesis-2026-05-24/README.md",
        doc_date=date(2026, 5, 24),
        debt_types=("a", "b"),
        statement=(
            "D44_late_convergence_carry — 'Live (KEEPER, Wilson 0.89)' (edge-synthesis "
            "2026-05-24) — вердикт на Wilson score directional accuracy, без Net ROI "
            "пересчёта и без control-group; аннулирован постмортемом "
            "backtest-prod-gap-is-metric-not-optimism-2026-06-24 (Wilson-vs-NetROI разрыв)"
        ),
        scope="detector:d44",
        quarantine_status="expired",
        evidence_note="README.md:58 — '| Late convergence carry | D44 | Live (KEEPER, Wilson 0.89) |'",
        already_closed_precedent=True,
        line_ref="README.md:58",
    ),
    # --- уже закрытый прецедент #2: drill-матрица не отмечена перед live D3 ---
    DebtItem(
        source_path="research/live-drill-tabletop-2026-05/",
        doc_date=date(2026, 5, 20),
        debt_types=("b",),
        statement=(
            "live-drill-tabletop-2026-05 задокументировал drill-матрицу (20-25 сценариев) "
            "2026-05-20, но research/live-drill-tabletop-2026-05/drill-reports/ остался "
            "пустым (только .gitkeep) на момент D3 live-запуска (2026-06-28, memory "
            "d3-live-blockers-cleared-2026-06-28) — pre-live чек-лист не был прогнан"
        ),
        scope="policy:live_readiness",
        quarantine_status="expired",
        evidence_note="drill-reports/ содержит только .gitkeep — drill не выполнялся",
        already_closed_precedent=True,
        line_ref="drill-reports/.gitkeep",
    ),
    # --- признак (а): win-rate-only вердикт с решающим порогом на WR_lo ---
    DebtItem(
        source_path="research/detector-audit-2026-05/report.md",
        doc_date=date(2026, 5, 20),
        debt_types=("a", "b"),
        statement=(
            "detector-audit-2026-05 (2026-05-20) — CUT/HOLD/KEEP-решения по всем "
            "детекторам на Wilson lower bound directional accuracy (WR_lo) + "
            "avg_roi (per-signal среднее, не bankroll-weighted портфельный ROI); "
            "напр. D38_resolution_lag 'best-in-class (WR_lo 86.6%, ROI +25.7%)' — "
            "тот же паттерн sum/mean-per-signal-ROI-vs-портфельный, что и подтверждённый "
            "баг D42 demote-алерта (2026-07-16, audit_case1)"
        ),
        scope="policy:detector_lifecycle",
        quarantine_status="expired",
        evidence_note="report.md:39-90 — CUT/KEEP пороги на WR_lo + avg_roi per-signal",
        line_ref="report.md:39,84,88",
    ),
    # --- признак (а): Win Rate таблица, явно "no fees applied" ---
    DebtItem(
        source_path="research/kronos-crypto-strike-2026-05/README.md",
        doc_date=date(2026, 5, 24),
        debt_types=("a",),
        statement=(
            "kronos-crypto-strike-2026-05 (закрыто 2026-05-24, 'do not repeat') — "
            "вердикт 'Win rate 32%... worse than coin flip' с явным 'No fees applied' "
            "(строка 92) — Net ROI net-of-fees не посчитан, решение принято на raw "
            "win rate + mean PnL без комиссии"
        ),
        scope="detector:kronos_crypto_strike",
        quarantine_status="expired",
        evidence_note="README.md:87-92 — Win rate таблица + 'No fees applied'",
        line_ref="README.md:87-92",
    ),
    # --- признак (а): "realized win rate per cal_p bin" как метод сравнения ---
    DebtItem(
        source_path="research/detector-rca-2026-05-26/README.md",
        doc_date=date(2026, 5, 26),
        debt_types=("a",),
        statement=(
            "detector-rca-2026-05-26 предлагает тест 'cal_p distribution vs realized "
            "win rate per cal_p bin (как G18 backtest)' (строка 109) как метод валидации "
            "калибровки — win-rate-based, не honest realized Net ROI"
        ),
        scope="policy:calibration",
        quarantine_status="expired",
        evidence_note="README.md:109",
        line_ref="README.md:109",
    ),
    # --- признак (а)+(в): market-calibration-baseline, сама граница даты ---
    DebtItem(
        source_path="research/market-calibration-baseline-2026-06-13.md",
        doc_date=date(2026, 6, 13),
        debt_types=("a",),
        statement=(
            "market-calibration-baseline-2026-06-13 — калибровочные перекосы найдены "
            "через Wilson 95% CI на actual_winrate vs implied_prob (mv_market_calibration), "
            "без Net ROI net-of-fees измерения; датирован ровно днём водораздела — "
            "включён в карантин по признаку (а), т.к. сам методологический скачок "
            "(whale-cluster-eda) документирован тем же днём, не этим файлом"
        ),
        scope="policy:calibration",
        quarantine_status="active",  # признак есть, но метод (Wilson CI, pure SQL MV) не тот же класс, что EDA-артефакт — ревалидация, не выброс
        evidence_note="строки 5,11,34-48 — Wilson CI на actual_winrate, без Net ROI",
        line_ref="market-calibration-baseline-2026-06-13.md:5,34",
    ),
    # --- признак (в): наивный EDA внутри самого граничного документа ---
    DebtItem(
        source_path="research/whale-cluster-eda-2026-06-13.md",
        doc_date=date(2026, 6, 13),
        debt_types=("c",),
        statement=(
            "whale-cluster-eda-2026-06-13 — наивный EDA-подраздел (строки 13-19, ДО "
            "look-ahead-safe пересчёта) дал FADE-сигнал 41 кластер edge -0.28 без "
            "time-aware split; сам документ явно маркирует это как артефакт ('Наивные "
            "цифры оставлены для истории/назидания', строка 19) — оставлен как пример "
            "долга внутри документа-водораздела, а не выброс всего файла (пересчитанный "
            "look-ahead-safe вывод того же документа уже честный)"
        ),
        scope="policy:look_ahead_discipline",
        quarantine_status="expired",
        evidence_note="строки 13-19 — наивный EDA без time-aware фильтра, N=41->4 после фикса",
        line_ref="whale-cluster-eda-2026-06-13.md:13-19",
    ),
    # --- 18 директорий research/*-2026-05 (суффикс только месяц, без дня) ---
    DebtItem(
        source_path="research/{adverse-selection-queue-position,b1-market-making,"
        "cross-venue-lead-lag,db-constraints-uniqueness,detector-audit,gamma-api,"
        "kalshi-integration,kalshi-moldova-availability,kronos-crypto-strike,"
        "late-convergence-carry,live-drill-tabletop,paired-tick-replay,"
        "polymarket-alpha-hypotheses,prediction-market-venues-api,"
        "risk-engine-production,shadow-gates-methodology,virtual-feedback,"
        "withdrawal-policy}-2026-05/",
        doc_date=date(2026, 5, 31),  # консервативно — последний день месяца, без дня в имени
        debt_types=("b", "c"),
        statement=(
            "18 директорий research/*-2026-05 (суффикс только месяц, без дня в имени) — "
            "весь май 2026, безусловно старше водораздела 2026-06-13; control-first как "
            "названная практика не документирована до июля (см. Step 1), поэтому "
            "признак (б) презюмируется по возрасту для всей группы без индивидуального "
            "разбора каждой директории (2 из 18 — detector-audit, kronos-crypto-strike — "
            "разобраны отдельно выше с конкретными строками; risk-engine-production, "
            "shadow-gates-methodology, virtual-feedback проверены и НЕ дали признаков "
            "(а)/(в) — не архитектурный/сигнальный ресёрч)"
        ),
        scope="policy:may_2026_batch",
        quarantine_status="expired",
        evidence_note="групповая запись — 18 директорий, датировка по имени каталога (месяц), не по дню",
        line_ref=None,
    ),
    # --- недатированные директории верхнего уровня + archive/, все март-апрель 2026 ---
    DebtItem(
        source_path="research/{archive/*,copy-trading-prediction-markets-whale-signals,"
        "oracle-edge-cluster-analysis,polymarket-accuracy-vs-polling-elections,"
        "polymarket-execution-bot,polymarket-orthogonal-strategies,"
        "prediction-market-edge-sources,"
        "novye-torgovye-strategii-prediction-markets-polymarket}/",
        doc_date=date(2026, 4, 16),  # самый поздний из проверенных git-first-commit дат группы
        debt_types=("b", "c"),
        statement=(
            "16 директорий без даты в имени (7 верхнего уровня + 9 в research/archive/) — "
            "git log --diff-filter=A показывает первый коммит в диапазоне 2026-03-22..2026-04-16, "
            "на 2+ месяца старше водораздела; групповая карантинная запись по тому же "
            "принципу возраста, что и May-batch выше, без построчного разбора (Acceptance "
            "брифа просит 5+ конкретных элементов, не полную опись каталога)"
        ),
        scope="policy:pre_may_2026_batch",
        quarantine_status="expired",
        evidence_note="git log --diff-filter=A first-commit dates, 2026-03-22..2026-04-16",
        line_ref=None,
    ),
    # --- MEMORY.md производная: подтверждение через ссылку на пре-cutoff research ---
    DebtItem(
        source_path="MEMORY.md#backtest-prod-gap-is-metric-not-optimism-2026-06-24",
        doc_date=date(2026, 6, 24),
        debt_types=("a",),
        statement=(
            "MEMORY.md: ни один topic-файл auto-memory не датирован раньше 2026-06-24 "
            "(самый ранний — backtest-prod-gap-is-metric-not-optimism-2026-06-24.md); "
            "Step 3 брифа (пройти MEMORY.md на признаки долга ДО водораздела) даёт "
            "пустой результат по самим topic-файлам — единственный релевантный сигнал "
            "в памяти — ссылки НА майские research-документы из более новых записей "
            "(см. D44-прецедент выше). Зафиксировано как явный null-результат, не как "
            "пропуск шага."
        ),
        scope="policy:memory_coverage",
        quarantine_status="active",  # это факт о структуре памяти, не долг сам по себе
        evidence_note="ls memory/*.md | grep -oE '2026-[0-9]{2}-[0-9]{2}' | sort — earliest 2026-06-24",
        line_ref=None,
        always_include=True,  # Step 3 meta-finding — валиден независимо от cutoff (сама запись про 2026-06-24)
    ),
)


def parse_date_from_filename(name: str) -> date | None:
    """Извлечь дату ГГГГ-ММ-ДД или ГГГГ-ММ (день=последний день месяца) из имени файла/директории.

    Чистая функция — Step 2 брифа: "файлы с датой в имени/frontmatter раньше
    водораздела". Поддерживает оба формата, встречающихся в research/:
    полная дата (`market-calibration-baseline-2026-06-13.md`,
    `edge-synthesis-2026-05-24`) и месяц-только (`detector-audit-2026-05`).
    Возвращает None, если дата в имени не найдена (недатированные директории —
    определяются отдельно через git history, не эту функцию).
    """
    import re

    full_match = re.search(r"(\d{4})-(\d{2})-(\d{2})(?:/|\.md)?$", name.rstrip("/"))
    if full_match:
        year, month, day = (int(g) for g in full_match.groups())
        try:
            return date(year, month, day)
        except ValueError:
            return None

    month_match = re.search(r"(\d{4})-(\d{2})$", name.rstrip("/"))
    if month_match:
        year, month = (int(g) for g in month_match.groups())
        if not 1 <= month <= 12:
            return None
        # Консервативно — последний день месяца (максимально близко к водоразделу,
        # не занижаем "возраст" находки).
        import calendar

        last_day = calendar.monthrange(year, month)[1]
        try:
            return date(year, month, last_day)
        except ValueError:
            return None

    return None


def is_before_cutoff(doc_date: date, cutoff: date = CUTOFF_DATE) -> bool:
    """Строго раньше водораздела (сам день водораздела НЕ карантинится по умолчанию по дате —
    см. market-calibration-baseline-2026-06-13, которая датирована РОВНО cutoff и разбирается
    индивидуально в реестре, а не автоматически этой функцией)."""
    return doc_date < cutoff


_KEYWORDS_A = ("win rate", "winrate", "win-rate", "wr_lo", "wilson", "hit rate")
_KEYWORDS_A_NEGATIVE = ("net roi", "net_roi", "net-of-fee", "honest realized")
_KEYWORDS_B = ("control group", "control-group", "baseline", "control ~")
_KEYWORDS_C = ("look-ahead", "look ahead", "time-aware", "walk-forward", "walk forward")
_KEYWORDS_C_NEGATIVE = ("no look-ahead", "look-ahead-safe", "anti-look-ahead")


def classify_debt_signals(text: str) -> tuple[DebtType, ...]:
    """Self-check классификатор (Step 2 признаки а/б/в) по ключевым словам в тексте.

    НЕ источник находок для DEBT_REGISTRY (см. docstring модуля — курируемый
    реестр, не рантайм-эвристика). Используется в тестах, чтобы проверить, что
    эвристика в принципе улавливает три признака брифа на реальных сниппетах
    из research/, и как задокументированная, воспроизводимая формализация того,
    что "признак" означает — не для слепого автоматического сканирования (слишком
    высокий false-positive rate на словах "control"/"Wilson" в непрофильном
    контексте, см. docstring модуля).

    Возвращает кортеж найденных типов долга ("a"/"b"/"c"), может быть пустым.
    """
    lowered = text.lower()
    found: list[DebtType] = []

    has_a = any(kw in lowered for kw in _KEYWORDS_A)
    has_a_mitigation = any(kw in lowered for kw in _KEYWORDS_A_NEGATIVE)
    if has_a and not has_a_mitigation:
        found.append("a")

    has_b = any(kw in lowered for kw in _KEYWORDS_B)
    if not has_b:
        # Отсутствие control/baseline упоминания — это ПРИЗНАК (б) отсутствует
        # явное сравнение, что как раз и есть долг. Явное упоминание control/
        # baseline снимает признак.
        found.append("b")

    has_c_risk = any(kw in lowered for kw in _KEYWORDS_C)
    has_c_mitigation = any(kw in lowered for kw in _KEYWORDS_C_NEGATIVE)
    if has_c_risk and not has_c_mitigation:
        found.append("c")

    return tuple(found)


def _config_hash(cutoff: date, registry: tuple[DebtItem, ...]) -> str:
    """sha256(cutoff + отсортированные statements реестра) — воспроизводимость без LLM."""
    payload = "\n---\n".join(
        [f"cutoff={cutoff.isoformat()}", *sorted(item.statement for item in registry)]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _dataset_hash(documents_scanned: int, registry_size: int) -> str:
    """sha256(f"{documents_scanned}:{registry_size}") — снапшот объёма скана.

    Не max(updated_at) как в соседних БД-кейсах — источник тут файловая
    система, а не таблица с timestamp-колонкой; воспроизводимость
    обеспечивается тем, что список файлов в research/ на момент прогона
    фиксирован (см. Acceptance брифа), не хешем содержимого файлов.
    """
    payload = f"{documents_scanned}:{registry_size}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class ScanResult:
    documents_scanned: int
    debt_items: list[DebtItem] = field(default_factory=list)


def scan(registry: tuple[DebtItem, ...] = DEBT_REGISTRY) -> ScanResult:
    """Step 2-3: применить курируемый реестр, отфильтровав по водоразделу.

    documents_scanned — оценка объёма прочитанного (не размер registry):
    бриф ожидает "около 35-45 записей" датированных файлов/директорий с
    полной датой + 18 майских директорий-без-дня + до 8/16 недатированных.
    Здесь фиксируем фактически проверенные группы Step 2 read-only-фазой
    2026-07-20 (см. docstring модуля): 16 полно-датированных файлов/директорий
    в research/ (до и после cutoff, все проверены на признаки) + 18
    майских-только-месяц директорий (групповая проверка) + 16 недатированных
    (7 top-level + 9 archive/, групповая проверка через git log) = 50.
    """
    documents_scanned = 50
    debt_items = [
        item
        for item in registry
        if item.always_include or is_before_cutoff(item.doc_date) or item.doc_date == CUTOFF_DATE
    ]
    return ScanResult(documents_scanned=documents_scanned, debt_items=debt_items)


def _build_verdict(result: ScanResult) -> str:
    """Свободный текст (фиксированный словарь §4.2 спеки — с Phase 1, не Phase 0).

    Open risk брифа: явно писать, что это ИНВЕНТАРИЗАЦИЯ (карантин-кандидаты
    на ревалидацию), НЕ окончательный вердикт "неверно" — числовой пересчёт
    каждого элемента остаётся работой других кейсов/будущих аудитов.
    """
    n_expired = sum(1 for i in result.debt_items if i.quarantine_status == "expired")
    n_active = sum(1 for i in result.debt_items if i.quarantine_status == "active")
    n_closed_precedent = sum(1 for i in result.debt_items if i.already_closed_precedent)
    lines = [
        "ИНВЕНТАРИЗАЦИЯ, НЕ ПРИГОВОР: sweep помечает кандидатов на ревалидацию "
        f"(cutoff={CUTOFF_DATE.isoformat()}), не выносит окончательный вердикт 'неверно' "
        "без реального пересчёта каждого элемента — это работа других кейсов/будущих аудитов.",
        f"documents_scanned={result.documents_scanned}, debt_items_found={len(result.debt_items)} "
        f"({n_expired} -> status=expired, {n_active} -> status=active с честным старым last_verified_at).",
        f"Из них {n_closed_precedent} — уже сработавшие прецеденты долга (D44 KEEPER->annulled, "
        "live-drill-tabletop матрица не отмечена перед D3 live) — подтверждают, что sweep-метод "
        "находит то, что реально было, не гипотетические риски.",
        "Признак (б) 'нет control/baseline' истинен почти повсеместно вплоть до июля 2026 "
        "(control-first как названная практика документирована только с "
        "research/fable-research-map-2026-07-19/ и research/d52-whale-flow-backtest-2026-07-09/) "
        "— это ожидаемый объём, не аномалия скана.",
    ]
    return "\n".join(lines)


async def run(*, dry_run: bool) -> dict[str, Any]:
    result = scan()
    verdict = _build_verdict(result)

    config_hash = _config_hash(CUTOFF_DATE, DEBT_REGISTRY)
    dataset_hash = _dataset_hash(result.documents_scanned, len(DEBT_REGISTRY))

    metrics: dict[str, Any] = {
        "cutoff_date": CUTOFF_DATE.isoformat(),
        "cutoff_rationale": CUTOFF_RATIONALE,
        "documents_scanned": result.documents_scanned,
        "debt_items_found": len(result.debt_items),
        "debt_type_labels": dict(_DEBT_LABELS),
        "quarantined_claim_ids": [],  # заполняется после INSERT ниже
        "debt_items": [
            {
                "source_path": item.source_path,
                "doc_date": item.doc_date.isoformat(),
                "debt_types": list(item.debt_types),
                "quarantine_status": item.quarantine_status,
                "already_closed_precedent": item.already_closed_precedent,
                "line_ref": item.line_ref,
            }
            for item in result.debt_items
        ],
    }

    print("=== May Debt Sweep — Phase 0 audit case 5 ===")
    print(f"cutoff_date={CUTOFF_DATE.isoformat()}")
    print(json.dumps(metrics, indent=2, default=str))
    print("\n--- verdict ---")
    print(verdict)

    if dry_run:
        print(f"\n[DRY] {len(result.debt_items)} debt items found. Skipping agent_runs/agent_claims writes.")
        for item in result.debt_items:
            print(f"  [{item.quarantine_status}] {item.source_path} ({','.join(item.debt_types)}) — {item.statement[:100]}...")
        return {"metrics": metrics, "verdict": verdict, "run_id": None}

    # Step 5: записать agent_runs (immutable run record) ПЕРВЫМ — run_id нужен
    # для evidence_run_ids каждого claim ниже.
    run_row = await fetchrow(
        """
        INSERT INTO agent_runs (kind, name, config_hash, dataset_hash, query_version, metrics, verdict, started_at, finished_at)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, now(), now())
        RETURNING id
        """,
        "dq",
        "may_debt_sweep",
        config_hash,
        dataset_hash,
        "v1",
        json.dumps(metrics, default=str),
        verdict,
    )
    run_id = int(run_row["id"])
    print(f"\nagent_runs.id={run_id}")

    # Step 4: race-safe upsert per debt item — ON CONFLICT (statement), работает
    # независимо от того, вставили ли уже похожий statement Task 3 (seed) или
    # replay-кейсы 1-4/9 (запущенные раньше по порядку, см. "Порядок и
    # параллелизация" брифа). status='expired' для устаревшего-и-не-
    # переверифицированного; status='active' с last_verified_at=doc_date (НЕ
    # now()!) для того, что осталось верным, но давно не пересчитывалось —
    # это то, что заставит claims_expiry_queue read model честно показать
    # протухшее.
    quarantined_claim_ids: list[int] = []
    for item in result.debt_items:
        last_verified_at = datetime(
            item.doc_date.year, item.doc_date.month, item.doc_date.day, tzinfo=UTC
        )
        claim_row = await fetchrow(
            """
            INSERT INTO agent_claims (statement, scope, status, evidence_run_ids, last_verified_at, verify_policy, verify_halflife_days)
            VALUES ($1, $2, $3, $4::bigint[], $5, $6, $7)
            ON CONFLICT (statement) DO UPDATE
            SET status = EXCLUDED.status,
                last_verified_at = EXCLUDED.last_verified_at,
                updated_at = now()
            RETURNING id
            """,
            item.statement,
            item.scope,
            item.quarantine_status,
            [run_id],
            last_verified_at,
            "time_halflife",
            90,
        )
        claim_id = int(claim_row["id"])
        quarantined_claim_ids.append(claim_id)
        print(f"agent_claims upserted id={claim_id} status={item.quarantine_status!r}: {item.statement[:100]}...")

    metrics["quarantined_claim_ids"] = quarantined_claim_ids
    await execute(
        "UPDATE agent_runs SET metrics = $1::jsonb WHERE id = $2",
        json.dumps(metrics, default=str),
        run_id,
    )

    return {"metrics": metrics, "verdict": verdict, "run_id": run_id}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Просканировать и распечатать найденные debt items, но не писать в agent_runs/agent_claims.",
    )
    return parser.parse_args()


async def main() -> None:
    args = _parse_args()

    if args.dry_run:
        # dry-run не требует БД вообще — чисто файловый скан + печать реестра.
        await run(dry_run=True)
        return

    settings = get_settings()
    dsn = settings.agent_database_url
    if not dsn:
        print(
            "WARNING: agent_database_url не задан — read-only роль oraclepoly_agent_ro "
            "ещё не сконфигурирована (Task 2). Скрипт НЕ должен молча уходить под "
            "основным DATABASE_URL для read-only аудита.",
            file=sys.stderr,
        )
        sys.exit(1)

    await init_db(dsn=dsn)
    try:
        await run(dry_run=False)
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
