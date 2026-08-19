"""Portfolio Analyst Agent — daily-бриф (Phase 0, БЕЗ action-кнопок).

Спека: docs/specs/portfolio_analyst_agent.md §8 — "daily-бриф без action-кнопок"
(Bounded Action Executor = Phase 3, `agent_actions` создана в Task 1, но не
используется до тех пор). Это НЕ LLM-синтезатор — Phase 0 не строит `app/agent/`
модуль (см. task-10-brief.md "Обоснование для реализатора"). Джоб чисто
детерминированный: читает agent_runs/claims_expiry_queue, форматирует markdown,
отправляет владельцу текстом через Telegram.

Содержит (Task 10 брифа Step 2):
  (а) agent_runs за последние 24ч — что прогнано (kind/name/verdict/id).
  (б) claims_expiry_queue WHERE expires_at < now() + 3 дня — протухающие клеймы.
  (в) DQ-статус — Phase 0 не имеет полноценного DQ-аудита (§4.2 GREEN|AMBER|RED
      по 6 проверкам — это app/services/detector_metrics.py-подобный модуль,
      Phase 1). Daily-бриф ограничивается тем, что уже есть: сколько аудитов
      прогнано и сколько replay-кейсов (agent_runs.kind='audit',
      name LIKE '%_replay' OR 'sweep'/'contribution' и т.п. — Task 4-9)
      зелёные/красные по verdict-тексту (эвристика на свободный текст, НЕ
      фиксированный словарь — тот появится в Phase 2).

Acceptance-требование §8 (обязательное): каждое число в брифе несёт
source/query hash — прямую ссылку на agent_runs.id или agent_claims.id, не
пересказ. Реализовано форматированием "(agent_runs.id=<N>)"/"(agent_claims.id=<N>)"
рядом с каждым числом, взятым из конкретной строки.

REFRESH MATERIALIZED VIEW claims_expiry_queue: решение — джоб сам вызывает
REFRESH перед чтением (не CONCURRENTLY, DB-роль джоба не read-only agent-роль,
см. ниже; вариант (а) из task-10-brief.md п.5). MV маленькая (agent_claims
Phase 0 — десятки строк), REFRESH раз в день не создаёт нагрузки. Альтернатива
(отдельный hourly refresh-джоб по образцу refresh_mv_event_outcome.py) избыточна
для Phase 0 масштаба — заведём, если Phase 1+ read models вырастут по объёму
или появятся другие консьюмеры MV, которым нужна более свежая версия, чем раз
в день.

DATABASE_URL vs agent_database_url: этот джоб использует ОБЫЧНЫЙ DATABASE_URL
(через cron_runner.py::run_job → init_db() без dsn=), а не read-only
agent_database_url. Обоснование: agent_database_url (Task 2, read-only роль
oraclepoly_agent_ro) предназначен для разовых read-only research-скриптов
Task 4-9 (`scripts/research/*.py`), запускаемых вручную/ad-hoc на VPS вне
основного cron-цикла (см. CLAUDE.md "scripts/research/*.py — one-off скрипты
для VPS"). Task 10 джобы — встроенные production cron-джобы, зарегистрированные
в scripts/cron_runner.py::COMMANDS наравне с daily_summary/db_size_report и
т.п., исполняются как часть основного backend-процесса. Кроме того, джоб
вызывает REFRESH MATERIALIZED VIEW — DDL-операция, недоступная read-only роли
(REVOKE/GRANT в 20260720133238 не выдаёт agent-роли CREATE/REFRESH привилегий
на public-схему сверх SELECT+CRUD на 4 agent_* таблицы). Обычный DATABASE_URL
здесь корректнее и достаточен.

Cron: daily, ПОСЛЕ существующего утреннего цикла аудитов/отчётов (после
db-size-report/detectors-honest-audit-report и т.п.), не пересекаясь по
времени — см. crontab.conf.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from app.analytics.ips import IpsParseError, load_ips_file
from app.analytics.policy_engine import VERDICT_INVESTIGATE_DATA
from app.services.database import execute, fetch, fetchrow

logger = structlog.get_logger(__name__)

# Путь к IPS-файлу владельца — та же конвенция, что и в остальном Phase 2 C2
# (файл в контейнере, бриф не ходит в БД за IPS-статусом).
_IPS_PATH = Path("owner_ips.local.md")

# Сколько протухающих клеймов показывать построчно в брифе — компромисс между
# полнотой и spam (по аналогии с N_TOP_DETECTORS в daily_summary.py).
N_EXPIRING_CLAIMS_SHOWN = 10

# agent_runs.name паттерны шести Phase 0 replay-кейсов (Task 4-9) — используются
# только для группировки в разделе "Replay-кейсы Phase 0", список свободного
# текста, НЕ формальный словарь (Phase 2 introduces один).
_REPLAY_CASE_NAMES = (
    "d42_portfolio_roi_replay",
    "d38_anomaly_stage_replay",
    "hold_vs_tpsl_replay",
    "g18_money_contribution",
    "may_debt_sweep",
    "uma_dispute_overlap_replay",
)

_RECENT_RUNS_SQL = """
SELECT id, kind, name, verdict, started_at, finished_at
FROM agent_runs
WHERE started_at > now() - INTERVAL '24 hours'
ORDER BY started_at DESC
"""

_EXPIRING_CLAIMS_SQL = """
SELECT id, statement, scope, expires_at, last_verified_at
FROM claims_expiry_queue
WHERE expires_at IS NOT NULL
  AND expires_at < now() + INTERVAL '3 days'
ORDER BY expires_at ASC
"""

_REFRESH_CLAIMS_EXPIRY_QUEUE_SQL = "REFRESH MATERIALIZED VIEW claims_expiry_queue"

# Все agent_runs.kind IN ('audit', 'dq') когда-либо прогнанные (не только за
# 24ч) — для раздела "какие 6 replay-кейсов зелёные/красные" (Task 10 брифа
# Step 2), который смотрит на факт наличия/вердикт последнего прогона каждого
# кейса, а не только на то, что случилось за последние сутки. 'dq' включён
# потому что audit_case5_may_debt_sweep.py (Task 8) намеренно пишет
# kind='dq' (data-quality sweep, не численный аудит) — без 'dq' в фильтре
# этот кейс никогда не появлялся бы в разделе "6 шт.", постоянно занижая
# счётчик до 5 из 6 (Task 10 review fix).
# name <> 'dq_audit_daily': dq_audit_daily (Phase 2 C0, docs/superpowers/specs/
# 2026-07-23-portfolio-agent-phase2-policy-engine-design.md) — operational DQ-мини-
# аудит прода (freshness/completeness/expired-share/referential integrity), НЕ
# replay-кейс Phase 0. У него свой TG-канал алертов (throttle на смене вердикта,
# см. app/jobs/dq_audit_daily.py), а _is_red_verdict ниже откалиброван на free-text
# verdict-тексты 6 replay-скриптов и вернёт False для голого 'RED'/'AMBER'/'GREEN' —
# без исключения живой DQ RED рендерился бы 🟢 в этом разделе. Отдельная строка
# DQ-статуса в брифе — скоуп Компонента C1 (§2.6 design doc), не C0.
_LATEST_AUDIT_RUNS_SQL = """
SELECT DISTINCT ON (name) id, name, verdict, started_at
FROM agent_runs
WHERE kind IN ('audit', 'dq') AND name <> 'dq_audit_daily'
ORDER BY name, started_at DESC
"""

# Phase 2 C1 (§2.6): реальный DQ-статус вместо суррогатной заглушки Phase 0.
_LATEST_DQ_RUN_SQL = """
SELECT id, verdict, started_at
  FROM agent_runs
 WHERE kind = 'dq' AND name = 'dq_audit_daily'
 ORDER BY started_at DESC
 LIMIT 1
"""

# Policy-engine proposals за 24ч (спека §2.6). Несмотря на имя (историческое —
# изначально ВСЕ proposals были в тени), после C3a часть реально уходит в TG:
# 🟡 DQ/IPS-parse (verdict=INVESTIGATE_DATA) — всегда; остальное — если
# metrics.shadow=false (policy_engine_enabled=True на момент записи). Поэтому
# тянем metrics->>'shadow' — без него _build_brief не отличит «молчит» от «ушло»
# и врёт (whole-branch ревью C3a fix).
_SHADOW_PROPOSALS_SQL = """
SELECT id, name, verdict, (metrics->>'shadow')::boolean AS shadow
  FROM agent_runs
 WHERE kind = 'proposal'
   AND query_version = 'policy_engine_daily_v1'
   AND started_at > now() - interval '24 hours'
 ORDER BY started_at DESC
"""


_FYI_QUEUE_SQL = """
SELECT id, digest_line
  FROM notification_fyi_queue
 WHERE consumed_at IS NULL
 ORDER BY created_at
 LIMIT 50
"""

# Помечаем ровно те id, что реально попали в отправленный текст. Пометить всю
# очередь скопом (WHERE consumed_at IS NULL) означало бы потерять строки, которые
# не влезли в LIMIT или пришли между SELECT и UPDATE — они не попали бы ни в этот
# бриф, ни в следующий.
_FYI_CONSUME_SQL = """
UPDATE notification_fyi_queue SET consumed_at = now() WHERE id = ANY($1::bigint[])
"""


def _is_red_verdict(verdict: str | None) -> bool:
    """Эвристика "красный" verdict по свободному тексту (Phase 0, НЕ словарь §4.2).

    ВРЕМЕННЫЙ ТЕКСТОВЫЙ СУРРОГАТ до Phase 2 policy engine (фиксированный словарь
    OBSERVE/INVESTIGATE_DATA/... из §4.2) — это НЕ меняется этой правкой. Меняется
    только то, что набор маркеров теперь выверен против РЕАЛЬНЫХ verdict-текстов,
    которые пишут все 6 Phase 0 replay-кейсов (`scripts/research/audit_case1..6_*.py`,
    `_build_verdict()`/verdict_lines/verdict_parts), а не придуман абстрактно.

    Найденные при построчной проверке негативные маркеры (Task 10 review fix,
    2026-07-20 — источники см. tests/test_agent_daily_brief.py::
    test_is_red_verdict_matches_real_case*):
      - audit_case2 (d38 anomaly replay): "ЛОЖНЫЙ KILL воспроизведён" — подтверждает
        баг; "ВНИМАНИЕ: D3 превышает порог" — неожиданный ложноположительный кейс.
      - audit_case3 (hold vs tpsl replay): "ВНИМАНИЕ: минимум одна альтернатива...
        превзошла hold-to-resolution" — негативная ветка (расхождение с ожидаемым
        клеймом).
      - audit_case1 (d42 portfolio ROI replay): "(ХУЖЕ," в строке месячной
        деградации (маркер якорит скобку+запятую — конкурирующая строка
        audit_case3 использует то же слово "хуже" в прозе для ПОЗИТИВНОГО
        исхода: "все хуже hold-to-resolution" = альтернативы хуже hold =
        hold победил, это желаемый результат, НЕ red); "ЦЕЛИКОМ делают
        убыток" про stop_loss-exits.
      - audit_case6 (uma dispute overlap): "НАЙДЕНО:" — ветка, где live-позиции
        реально пересеклись с disputed-рынком (риск материализовался).

    audit_case4 (g18) и audit_case5 (may debt sweep) — verdict-тексты этих двух
    кейсов ИНФОРМАЦИОННЫЕ/counterfactual-измерительные (verdict_tag
    "counterfactual_unmeasured_in_reality", "ИНВЕНТАРИЗАЦИЯ, НЕ ПРИГОВОР"), без
    выраженного red/green исхода в тексте — намеренно не притягиваем для них
    маркер, чтобы не давать ложных 🔴 на нейтральных отчётах.

    Старые маркеры "баг подтверждён"/"bug confirmed" убраны — ни один из 6
    реальных скриптов не пишет такую строку буквально (grep подтвердил), эти
    маркеры ловили только придуманные тестовые строки, создавая ложное чувство
    покрытия. "refuted"/"annulled"/"не подтвержд"/"false" сохранены — не найдены
    в текущих 6 кейсах, но остаются на будущее как разумные red-сигналы для
    следующих replay-кейсов (Phase 0 не ограничивается 6 текущими).
    """
    if not verdict:
        return False
    v = verdict.lower()
    red_markers = (
        "ложный kill",
        "внимание:",
        "(хуже,",  # audit_case1 деградация: f"({'ХУЖЕ' if degraded else 'не хуже'}, ...)"
        "целиком делают убыток",
        "refuted",
        "annulled",
        "не подтвержд",
        "false",
    )
    if any(m in v for m in red_markers):
        return True
    # "найдено:" отдельно от остальных: audit_case6 пишет и "НАЙДЕНО:" (overlap
    # есть → red), и "НЕ НАЙДЕНО:" (overlap нет → green) — "найдено:" целиком
    # входит в "не найдено:" как substring, обычный substring-check дал бы red
    # на обеих ветках. Явно исключаем отрицание перед маркером.
    return "найдено:" in v and "не найдено:" not in v


def _ips_file_status() -> str:
    """'active' | 'absent' | 'parse_error' — для nag-строки брифа (спека §2 п.8).

    Любой сбой чтения (PermissionError, UnicodeDecodeError, …) деградирует в
    'parse_error', не роняя весь бриф — как и остальные источники модуля,
    изолированные try/except.
    """
    try:
        policy = load_ips_file(_IPS_PATH)
    except IpsParseError:
        return "parse_error"
    except Exception:
        logger.exception("agent_daily_brief_ips_status_failed")
        return "parse_error"
    return "absent" if policy is None else "active"


def _build_brief(
    recent_runs: list[dict[str, Any]],
    expiring_claims: list[dict[str, Any]],
    latest_audit_runs: list[dict[str, Any]],
    now: datetime,
    dq_status: dict[str, Any] | None,
    shadow_proposals: list[dict[str, Any]],
    ips_status: str = "absent",
    fyi_lines: list[dict[str, Any]] | None = None,
) -> str:
    """Pure formatter — без I/O, тестируемый отдельно.

    Каждое число сопровождается прямой ссылкой на agent_runs.id/agent_claims.id
    (§8 acceptance-требование "source/query hash", не пересказ).

    fyi_lines — готовые строки из notification_fyi_queue: это и есть обещанное
    спекой §4 «FYI строкой в Daily Brief вместо отдельного сообщения». Строки
    приходят уже отрендеренными и экранированными (render_digest_line), бриф их
    не форматирует повторно — граница §6.
    """
    lines = [
        "🕵️ <b>Portfolio Analyst Agent — Daily Brief</b>",
        f"<i>{now.strftime('%Y-%m-%d %H:%M UTC')}</i>",
        "",
        "<b>Прогоны за 24ч</b>",
    ]

    if not recent_runs:
        lines.append("  Нет прогонов за последние 24ч.")
    else:
        for r in recent_runs:
            verdict_short = (r["verdict"] or "—")[:120]
            lines.append(f"  • [{r['kind']}] {r['name']}: {verdict_short} (agent_runs.id={r['id']})")

    lines.extend(["", "<b>Replay-кейсы Phase 0 (6 шт., последний прогон каждого)</b>"])
    if not latest_audit_runs:
        lines.append("  Ни один кейс ещё не прогнан.")
    else:
        # _LATEST_AUDIT_RUNS_SQL уже DISTINCT ON (name) — каждое имя ровно раз.
        n_green = 0
        n_red = 0
        for r in latest_audit_runs:
            is_red = _is_red_verdict(r["verdict"])
            n_red += int(is_red)
            n_green += int(not is_red)
            marker = "🔴" if is_red else "🟢"
            verdict_short = (r["verdict"] or "—")[:100]
            lines.append(f"  {marker} {r['name']}: {verdict_short} (agent_runs.id={r['id']})")
        n_missing = max(0, len(_REPLAY_CASE_NAMES) - len(latest_audit_runs))
        lines.append(
            f"  Итого: {n_green} зелёных, {n_red} красных из {len(latest_audit_runs)} прогнанных "
            f"({n_missing} из 6 ещё не прогнаны)"
            if n_missing > 0
            else f"  Итого: {n_green} зелёных, {n_red} красных из {len(latest_audit_runs)} прогнанных"
        )

    lines.extend(["", "<b>Протухающие клеймы (&lt;3 дня)</b>"])
    if not expiring_claims:
        lines.append("  Нет клеймов, протухающих в ближайшие 3 дня.")
    else:
        for c in expiring_claims[:N_EXPIRING_CLAIMS_SHOWN]:
            statement_short = c["statement"][:100]
            expires = c["expires_at"].strftime("%Y-%m-%d") if c["expires_at"] else "—"
            lines.append(f"  • {statement_short} — expires {expires} (agent_claims.id={c['id']})")
        if len(expiring_claims) > N_EXPIRING_CLAIMS_SHOWN:
            lines.append(f"  … и ещё {len(expiring_claims) - N_EXPIRING_CLAIMS_SHOWN}")

    lines.extend(["", "<b>DQ-статус</b>"])
    if dq_status is None:
        lines.append("  DQ-run не найден — dq_audit_daily ещё не прогнан (cron 06:45 UTC).")
    else:
        marker = {"GREEN": "🟢", "AMBER": "🟡", "RED": "🔴"}.get(str(dq_status["verdict"]), "⚪")
        ts = dq_status["started_at"].strftime("%Y-%m-%d %H:%M UTC")
        lines.append(f"  {marker} {dq_status['verdict']} — {ts} (agent_runs.id={dq_status['id']})")

    lines.extend(["", "<b>Policy engine (shadow)</b>"])
    if not shadow_proposals:
        lines.append("  0 proposals за 24ч.")
    else:
        # C3a: 🟡 DQ/IPS-parse (verdict=INVESTIGATE_DATA) уходит в TG ВСЕГДА (спека §5),
        # остальное — только если metrics.shadow=false (enabled=True в момент записи).
        # Раньше строка безусловно писала "TG молчит — shadow" для всех proposals —
        # враньё для 🟡, которые реально доходят до владельца (whole-branch ревью C3a fix).
        def _was_sent(p: dict[str, Any]) -> bool:
            if p.get("verdict") == VERDICT_INVESTIGATE_DATA:
                return True
            return p.get("shadow") is False

        n_sent = sum(1 for p in shadow_proposals if _was_sent(p))
        n_silent = len(shadow_proposals) - n_sent
        lines.append(
            f"  {len(shadow_proposals)} proposals за 24ч ({n_silent} в тени 🟠, не отправлены; "
            f"{n_sent} отправлено 🟡/live):"
        )
        for p in shadow_proposals[:10]:
            marker = "→ ушло" if _was_sent(p) else "в тени"
            lines.append(f"  • {p['verdict']} — {p['name']} ({marker}) (agent_runs.id={p['id']})")
        if len(shadow_proposals) > 10:
            lines.append(f"  … и ещё {len(shadow_proposals) - 10}")

    if ips_status == "absent":
        lines.append(
            "  ⚠️ IPS не заполнен (owner_ips.local.md) — IPS-гейт неактивен. Скопируй owner_ips.template.md и заполни."
        )
    elif ips_status == "parse_error":
        lines.append(
            "  ⚠️ IPS не парсится (owner_ips.local.md) — IPS-гейт неактивен. См. лог policy_engine_ips_parse_error."
        )

    if fyi_lines:
        lines.extend(["", "<b>Сводка за сутки</b>"])
        lines.extend(f"  {row['digest_line']}" for row in fyi_lines)

    return "\n".join(lines)


async def run_agent_daily_brief() -> dict[str, Any]:
    """Собрать daily-бриф Portfolio Analyst Agent и отправить admin'ам.

    REFRESH claims_expiry_queue делается здесь перед чтением (см. docstring
    модуля) — обычный DATABASE_URL (не read-only agent-роль), поэтому REFRESH
    доступен.
    """
    try:
        await execute(_REFRESH_CLAIMS_EXPIRY_QUEUE_SQL)
    except Exception:
        # claims_expiry_queue создаётся отдельной миграцией (Step 1) — если
        # миграция ещё не применена на этом окружении, REFRESH упадёт.
        # Не валим весь бриф — просто читаем что есть (SELECT ниже тоже
        # упадёт в этом случае и вернёт []).
        logger.exception("agent_daily_brief_refresh_failed")

    recent_runs = [dict(r) for r in await fetch(_RECENT_RUNS_SQL)]
    latest_audit_runs = [dict(r) for r in await fetch(_LATEST_AUDIT_RUNS_SQL)]
    try:
        expiring_claims = [dict(r) for r in await fetch(_EXPIRING_CLAIMS_SQL)]
    except Exception:
        logger.exception("agent_daily_brief_expiring_claims_query_failed")
        expiring_claims = []
    try:
        dq_row = await fetchrow(_LATEST_DQ_RUN_SQL)
        dq_status = dict(dq_row) if dq_row else None
    except Exception:
        logger.exception("agent_daily_brief_dq_status_query_failed")
        dq_status = None
    try:
        shadow_proposals = [dict(r) for r in await fetch(_SHADOW_PROPOSALS_SQL)]
    except Exception:
        logger.exception("agent_daily_brief_shadow_proposals_query_failed")
        shadow_proposals = []
    try:
        fyi_lines = [dict(r) for r in await fetch(_FYI_QUEUE_SQL)]
    except Exception:
        logger.exception("agent_daily_brief_fyi_queue_query_failed")
        fyi_lines = []

    now = datetime.now(UTC)
    text = _build_brief(
        recent_runs,
        expiring_claims,
        latest_audit_runs,
        now,
        dq_status,
        shadow_proposals,
        ips_status=_ips_file_status(),
        fyi_lines=fyi_lines,
    )

    # ЕДИНСТВЕННОЕ исключение из правила «джоба очереди 1 не шлёт в TG напрямую»
    # (Acceptance §9). Daily Brief не источник уведомлений, а КОНЕЧНАЯ ТОЧКА
    # FYI-потока: он забирает накопленное из notification_fyi_queue и печатает
    # одним сообщением. Пропустить его собственный текст через шину нельзя —
    # получилась бы рекурсия: бриф публикует себя → строка ложится в очередь →
    # следующий бриф её забирает и снова публикует себя. Поэтому здесь прямая
    # отправка by design, и pre-commit guard обязан держать этот файл в
    # белом списке.
    from app.tasks.notifications import send_admin_message

    await send_admin_message(text)

    # Помечаем consumed ТОЛЬКО после успешной отправки: сбой доставки не должен
    # «съедать» очередь — иначе FYI за сутки исчезают, а до владельца не дошли.
    # Исключение из send_admin_message пробрасывается наружу намеренно, чтобы
    # cron-раннер зафиксировал сбой, а строки остались для следующего прогона.
    if fyi_lines:
        try:
            await execute(_FYI_CONSUME_SQL, [row["id"] for row in fyi_lines])
        except Exception:
            # Бриф уже доставлен — повторная пометка случится в следующий прогон,
            # ценой повтора строк в одном брифе. Это дешевле, чем упасть после
            # отправки и потерять сам факт успешной доставки.
            logger.exception("agent_daily_brief_fyi_consume_failed")

    logger.info(
        "agent_daily_brief_sent",
        n_recent_runs=len(recent_runs),
        n_expiring_claims=len(expiring_claims),
        n_audit_cases=len(latest_audit_runs),
        n_fyi=len(fyi_lines),
    )
    return {
        "n_recent_runs": len(recent_runs),
        "n_expiring_claims": len(expiring_claims),
        "n_audit_cases": len(latest_audit_runs),
        "n_fyi": len(fyi_lines),
    }


logging.basicConfig(level=logging.INFO)
