"""Policy engine daily — джоба C1+C2 (Phase 2).

Читает: свежий DQ-run C0, свежие runs мониторов Phase 1 (DISTINCT ON name),
detector_state.stage, live-агрегат execution_orders. Прогоняет evaluate(),
пишет proposals в agent_runs(kind='proposal') + BLOCKED_DATA-клеймы при DQ RED.
DQ-run читается вместе с metrics.checks[] — причины сработавших проверок попадают
в текст advisory (спека docs/superpowers/specs/2026-07-30-dq-reason-detail-in-advisory-design.md).
Форма checks[] — межкомпонентный контракт с C0: переименование ключей в
dq_audit_daily._persist_run тихо выродит advisory в фолбэк-текст.
C2: IPS-гейт (owner_ips.local.md) — нарушение блокирует risk-вердикты как DQ RED;
файла нет → гейт неактивен.

C3a: после persist шлёт advisory-сообщения (proposal_render) по written proposals;
policy_engine_enabled=False → 🟠 молчит, 🟡 DQ уходит всегда (спека §5).
Cron-порядок: dq-audit 06:45 → edge-decay 07:00 → adverse-selection 07:15 → policy-engine 07:30,
чтобы читать runs текущего утра (см. комментарий app/jobs/dq_audit_daily.py).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import asyncpg

from app.analytics.action_gates import KIND_PAUSE_DETECTOR, KIND_REDUCE_CAP, snapshot_hash
from app.analytics.ips import (
    INACTIVE_IPS_GATE,
    IpsGate,
    IpsParseError,
    evaluate_ips_gate,
    load_ips_file,
)
from app.analytics.policy_engine import (
    _DEFAULT_PARAMS,
    VERDICT_INVESTIGATE_DATA,
    VERDICT_OBSERVE,
    DqCheckLine,
    DqStatus,
    LiveDrawdown,
    MonitorSignal,
    PolicyThresholds,
    Proposal,
    evaluate,
    proposal_id,
)
from app.analytics.proposal_render import render_proposal, should_send
from app.core.config import get_settings
from app.services.database import execute, fetch, fetchrow

logger = logging.getLogger(__name__)

_RUN_NAME = "policy_engine_daily"

_RULE_SQL = "SELECT version, params FROM policy_rules WHERE rule_key = 'policy_engine' AND active LIMIT 1"

_DQ_SQL = """
SELECT id, verdict, metrics
  FROM agent_runs
 WHERE name = 'dq_audit_daily' AND kind = 'dq'
 ORDER BY started_at DESC
 LIMIT 1
"""

# Свежайший run каждого монитора за 24ч (защита от протухших: старше суток не читаем).
# 24ч — осознанный allowance, не менять; _STALE_MONITOR_HOURS ниже лишь логирует наблюдаемость,
# если утренний монитор упал и тихо подставился вчерашний run.
_MONITOR_RUNS_SQL = """
SELECT DISTINCT ON (name) id, name, verdict, started_at
  FROM agent_runs
 WHERE kind = 'monitoring'
   AND started_at > now() - interval '24 hours'
 ORDER BY name, started_at DESC
"""

_STALE_MONITOR_HOURS = 8

_STAGES_SQL = "SELECT detector, stage FROM detector_state"

# Bankroll-weighted live-агрегат: SUM(net_pnl)/SUM(stake), НЕ sum-per-signal (инцидент D42).
# Скоуп по фактическим live-ордерам, не по detector_state.stage: детектор мог уйти из live,
# но недавняя live-история должна ловиться (прод-факт 2026-07-23: D3 в virtual, live ROI −33%).
_DRAWDOWN_SQL = """
SELECT detector,
       count(*)      AS n_settled,
       SUM(net_pnl)  AS net_pnl,
       SUM(stake)    AS stake
  FROM execution_orders
 WHERE execution_mode IN ('maker', 'taker')
   AND status = 'settled'
   AND settled_at > now() - ($1 || ' days')::interval
   AND detector IS NOT NULL
 GROUP BY detector
"""

_IPS_PATH = Path("owner_ips.local.md")  # cwd контейнера = /app = корень репо; gitignored
_IPS_PARSE_COOLDOWN_HOURS = 168  # спека §5: парс-ошибка → 🟡 однократно (cooldown 7д)

# Live-стейк по категориям за окно drawdown'а — для проверки forbidden_categories IPS.
# JOIN markets по паттерну adverse_selection_monitor (m.polymarket_id = eo.market_id —
# detector_signals ID-конвенция: market_id это polymarket_id 0x, не внутренний UUID markets.id;
# эталон _CELL_ROWS_SQL в app/jobs/adverse_selection_monitor.py).
# Стейк live-ордеров окна, кроме никогда не исполнявшихся (rejected/cancelled); settled включён
# намеренно — консервативный гейт по окну drawdown-правила (недавняя закрытая торговля в запретной
# категории — тоже нарушение политики, а не только текущая открытая экспозиция, by design).
_FORBIDDEN_EXPOSURE_SQL = """
SELECT COALESCE(m.category, 'other') AS category,
       SUM(eo.stake)                 AS stake
  FROM execution_orders eo
  JOIN markets m ON m.polymarket_id = eo.market_id
 WHERE eo.execution_mode IN ('maker', 'taker')
   AND eo.created_at > now() - ($1 || ' days')::interval
   AND eo.status NOT IN ('rejected', 'cancelled')
 GROUP BY 1
"""


async def _load_rule() -> tuple[int, PolicyThresholds]:
    """Версия+пороги; фолбэк на defaults (паттерн dq_audit_daily._load_rule)."""
    try:
        row = await fetchrow(_RULE_SQL)
    except asyncpg.UndefinedTableError:
        logger.warning("policy_engine_rule_table_missing_using_defaults")
        return 0, PolicyThresholds.from_params(_DEFAULT_PARAMS)
    if row is None:
        logger.warning("policy_engine_rule_missing_using_defaults")
        return 0, PolicyThresholds.from_params(_DEFAULT_PARAMS)
    params = row["params"]
    if isinstance(params, str):
        params = json.loads(params)
    return int(row["version"]), PolicyThresholds.from_params(params)


def _parse_dq_checks(metrics: object) -> tuple[DqCheckLine, ...]:
    """metrics.checks[] → DqCheckLine. Любое непригодное содержимое → () (спека §3 п.4).

    jsonb из asyncpg приходит либо str, либо dict (паттерн dq_audit_daily.py:132).
    Ключи читаются через .get: форма checks[] — межкомпонентный контракт с C0
    (спека §6), и её поломка не должна ронять джобу.
    """
    if isinstance(metrics, str):
        try:
            metrics = json.loads(metrics)
        except ValueError:
            logger.warning("policy_engine_dq_metrics_unparsable")
            return ()
    if not isinstance(metrics, dict):
        return ()
    raw = metrics.get("checks")
    if not isinstance(raw, list):
        return ()
    lines = [
        DqCheckLine(check=str(c["check"]), verdict=str(c.get("verdict", "")), detail=str(c.get("detail", "")))
        for c in raw
        if isinstance(c, dict) and c.get("check")
    ]
    if len(lines) != len(raw):
        logger.warning("policy_engine_dq_checks_partially_malformed n_raw=%d n_ok=%d", len(raw), len(lines))
    return tuple(lines)


async def _fetch_dq_status() -> DqStatus:
    """Свежий DQ-вердикт C0 + детали проверок; run отсутствует → RED (fail-closed).

    checks нужны для причины в advisory (спека §4); пустые — сообщение уходит
    с фолбэк-текстом, но уходит.
    """
    row = await fetchrow(_DQ_SQL)
    if row is None:
        logger.warning("policy_engine_no_dq_run_fail_closed")
        return DqStatus(verdict="RED", run_id=None)
    checks = _parse_dq_checks(row.get("metrics"))
    if not checks:
        logger.warning("policy_engine_dq_details_unavailable run_id=%s", row["id"])
    return DqStatus(verdict=str(row["verdict"]), run_id=int(row["id"]), checks=checks)


async def _fetch_detector_stages() -> dict[str, str]:
    rows = await fetch(_STAGES_SQL)
    return {str(r["detector"]): str(r["stage"]) for r in rows}


def _parse_monitor_name(name: str) -> tuple[str, str, str | None] | None:
    """agent_runs.name монитора → (monitor, entity, category) или None (не-детекторный run)."""
    if name.startswith("edge_decay_monitor:detector:"):
        return "edge_decay", name.removeprefix("edge_decay_monitor:detector:"), None
    if name.startswith("adverse_selection_monitor:"):
        rest = name.removeprefix("adverse_selection_monitor:")
        if rest == "run_manifest" or "×" not in rest:
            return None
        entity, category = rest.split("×", 1)
        return "adverse_selection", entity, category
    return None  # edge_decay methodology-runs и прочее — не входы правил v1


async def _fetch_monitor_signals(stages: dict[str, str]) -> list[MonitorSignal]:
    rows = await fetch(_MONITOR_RUNS_SQL)
    signals: list[MonitorSignal] = []
    now = datetime.now(UTC)
    for r in rows:
        started_at = r["started_at"]
        if started_at is not None:
            age_h = (now - started_at).total_seconds() / 3600
            if age_h > _STALE_MONITOR_HOURS:
                logger.warning("policy_engine_stale_monitor_run name=%s age_h=%.1f", r["name"], age_h)
        parsed = _parse_monitor_name(str(r["name"]))
        if parsed is None:
            continue
        monitor, entity, category = parsed
        signals.append(
            MonitorSignal(
                monitor=monitor,
                entity=entity,
                category=category,
                verdict=str(r["verdict"]),
                run_id=int(r["id"]),
                stage=stages.get(entity),
            )
        )
    return signals


async def _fetch_live_drawdowns(window_days: int) -> list[LiveDrawdown]:
    rows = await fetch(_DRAWDOWN_SQL, str(window_days))
    return [
        LiveDrawdown(
            detector=str(r["detector"]),
            n_settled=int(r["n_settled"]),
            net_pnl=float(r["net_pnl"] or 0),
            stake=float(r["stake"] or 0),
        )
        for r in rows
    ]


async def _persist_ips_parse_marker(dq_verdict: str) -> None:
    """Спека §5: IPS-файл не парсится → 🟡 однократно (cooldown 7д), уходит в TG.

    Cooldown — time-based по name (не по оконному хэшу: смена ISO-недели на
    границе вс→пн меняла бы pid через день); оконный pid остаётся вторым
    слоем — ON CONFLICT против гонки параллельных прогонов.

    IPS-parse — 🟡 DQ-подобный класс («данные/конфиг подозрительны», спека §5)
    → уходит в TG ВСЕГДА, независимо от policy_engine_enabled (как DQ-🟡),
    но только когда маркер реально только что вставлен (cooldown-guard же
    гарантирует однократность отправки за 7д — тот же guard, что и INSERT).

    dq_verdict — реальный DQ-вердикт цикла (из _fetch_dq_status), прокинутый через
    _load_ips_gate. Раньше hardcode "GREEN" рисовал в evidence-строке "DQ 🟢",
    даже когда DQ был RED/AMBER — вводило в заблуждение (whole-branch ревью C3a).
    """
    from app.tasks.notifications import send_admin_message

    window = datetime.now(UTC).strftime("%G-W%V")
    pid = proposal_id("IPS_PARSE_ERROR", "policy:ips_parse", 0, window)
    if await fetchrow(_IPS_PARSE_COOLDOWN_BY_NAME_SQL, str(_IPS_PARSE_COOLDOWN_HOURS)) is not None:
        return
    metrics = json.dumps({"reason": "owner_ips.local.md exists but does not parse", "shadow": True})
    status = await execute(
        _INSERT_PROPOSAL_SQL,
        "policy_engine:policy:ips_parse",
        pid,
        "rule_v0",
        "policy_engine_daily_v1",
        metrics,
        VERDICT_INVESTIGATE_DATA,
    )
    if status.endswith(" 0"):
        return  # ON CONFLICT DO NOTHING — гонка с параллельным прогоном, не дублировать отправку
    p = Proposal(
        verdict=VERDICT_INVESTIGATE_DATA,
        scope="policy:ips_parse",
        reason="owner_ips.local.md exists but does not parse",
        rule_version=0,
    )
    text = render_proposal(p, dq_verdict, None)
    if text is not None:
        await send_admin_message(text)


async def _load_ips_gate(drawdowns: list[LiveDrawdown], window_days: int, dq_verdict: str = "GREEN") -> IpsGate:
    """Файл → парс → exposure → IpsGate. Нет файла/парс-ошибка → гейт неактивен (спека §5).

    window_days — то же ЗНАЧЕНИЕ окна, что у drawdown-правила; фактические множества
    строк различаются осознанно: exposure — по created_at и status NOT IN
    (rejected,cancelled), drawdown — по settled_at и status='settled'.

    dq_verdict — реальный DQ-вердикт цикла (от _fetch_dq_status в run_policy_engine_daily),
    нужен только для честной evidence-строки IPS-parse-🟡-маркера (см. _persist_ips_parse_marker).
    Дефолт "GREEN" — для существующих вызовов/тестов, не проверяющих эту ветку.
    """
    try:
        policy = load_ips_file(_IPS_PATH)
    except IpsParseError:
        logger.warning("policy_engine_ips_parse_error_gate_inactive", exc_info=True)
        await _persist_ips_parse_marker(dq_verdict)
        return INACTIVE_IPS_GATE
    if policy is None:
        logger.info("policy_engine_ips_absent_gate_inactive")
        return INACTIVE_IPS_GATE

    rows = await fetch(_FORBIDDEN_EXPOSURE_SQL, str(window_days))
    exposure = {str(r["category"]).lower(): float(r["stake"] or 0) for r in rows}

    total_pnl = sum(d.net_pnl for d in drawdowns)
    total_stake = sum(d.stake for d in drawdowns)
    portfolio_roi = (total_pnl / total_stake) if total_stake > 0 else None

    return evaluate_ips_gate(policy, exposure, portfolio_roi)


_INSERT_PROPOSAL_SQL = """
INSERT INTO agent_runs (kind, name, config_hash, dataset_hash, query_version, metrics, verdict, finished_at)
VALUES ('proposal', $1, $2, $3, $4, $5::jsonb, $6, now())
ON CONFLICT (config_hash) WHERE kind = 'proposal' AND query_version = 'policy_engine_daily_v1' DO NOTHING
"""

_COOLDOWN_SQL = """
SELECT 1
  FROM agent_runs
 WHERE kind = 'proposal'
   AND query_version = 'policy_engine_daily_v1'
   AND name = $1
   AND verdict = $2
   AND started_at > now() - ($3 || ' hours')::interval
 LIMIT 1
"""

_IPS_PARSE_COOLDOWN_BY_NAME_SQL = """
SELECT 1
  FROM agent_runs
 WHERE kind = 'proposal'
   AND name = 'policy_engine:policy:ips_parse'
   AND started_at > now() - ($1 || ' hours')::interval
 LIMIT 1
"""

# Phase 3: L2-действие, привязанное к proposal. TTL 24ч (спека §4) — окно перекрывает
# суточный интервал джобы, чтобы кнопка не умирала до следующего сообщения.
_INSERT_L2_ACTION_SQL = """
INSERT INTO agent_actions
    (kind, level, status, actor, payload, payload_hash, idempotency_key, expires_at)
VALUES ($1, 'L2', 'proposed', 'policy_engine', $2::jsonb, $3, $4, now() + interval '24 hours')
ON CONFLICT (idempotency_key) DO NOTHING
"""

_L2_SNAPSHOT_SQL = "SELECT stage, live_stake_cap_usd FROM detector_state WHERE detector = $1"

_BLOCKED_CLAIM_SQL = """
INSERT INTO agent_claims (statement, scope, status, evidence_run_ids, verify_policy, verify_halflife_days)
VALUES ($1, $2, 'active', $3::bigint[], 'time_halflife', 7)
ON CONFLICT (statement) DO UPDATE
   SET evidence_run_ids = agent_claims.evidence_run_ids || EXCLUDED.evidence_run_ids,
       last_verified_at = now(),
       updated_at = now()
"""


async def _persist_proposals(
    proposals: list[Proposal], cooldown_hours: int, ips_version: str | None, enabled: bool
) -> tuple[list[tuple[Proposal, str, bool]], int]:
    """Пишет не-blocked proposals в agent_runs(kind='proposal') с time-based cooldown.

    Cooldown — time-based по (name, verdict), НЕ по оконному config_hash. Оконный pid
    содержит календарный день, поэтому на суточной джобе он менялся каждый прогон:
    _COOLDOWN_SQL искал хэш, которого ещё нет в таблице, и cooldown не срабатывал
    никогда (6 побайтово одинаковых advisory про D3 за 9 дней тени, 2026-08-01).
    Тот же урок уже зафиксирован в _persist_ips_parse_marker.

    verdict в ключе — обязателен: cooldown только по name глушил бы СМЕНУ вердикта
    для того же scope (D3 PAUSE_REVIEW → CLOSE_POSITION_REVIEW), а эскалация это
    новое событие и должна доходить сразу.

    Оконный pid остаётся вторым слоем — ON CONFLICT против гонки параллельных прогонов
    (снять окно из хэша нельзя: UNIQUE(config_hash) превратил бы cooldown в вечный бан).

    Возвращает список троек (proposal, pid, has_l2) РЕАЛЬНО записанных proposals (для шлюза
    отправки — pid нужен для callback_data L1-кнопок, спека C3b §3.4; has_l2 — Phase 3, заведена
    ли парная L2-строка в agent_actions для кнопки «⚡ Сделать») + skipped-счётчик.
    metrics.shadow = not enabled — исторический факт «TG молчал ли на момент записи».

    L2-строка (Phase 3) заводится здесь, а не в _send_advisory: эта функция уже ходит в БД
    (fetchrow для cooldown, execute для вставки proposal), а _send_advisory — чистая функция
    отправки в Telegram без обращений к БД; заводить их там означало бы новый I/O в месте,
    чьи тесты его не мокают.
    """
    window = datetime.now(UTC).strftime("%Y-%m-%d")
    written: list[tuple[Proposal, str, bool]] = []
    skipped = 0
    for p in proposals:
        if p.blocked:
            continue  # blocked → _persist_blocked_claims, не proposal
        pid = proposal_id(p.verdict, p.scope, p.rule_version, window)
        if await fetchrow(_COOLDOWN_SQL, f"policy_engine:{p.scope}", p.verdict, str(cooldown_hours)) is not None:
            skipped += 1
            continue
        metrics = json.dumps(
            {
                "reason": p.reason,
                "evidence_run_ids": list(p.evidence_run_ids),
                "rule_version": p.rule_version,
                "blocked": False,
                "shadow": not enabled,
                "ips_version": ips_version,  # спека §9.1: каждый proposal ссылается на версию IPS
            }
        )
        status = await execute(
            _INSERT_PROPOSAL_SQL,
            f"policy_engine:{p.scope}",
            pid,
            f"rule_v{p.rule_version}",
            "policy_engine_daily_v1",
            metrics,
            p.verdict,
        )
        # ON CONFLICT DO NOTHING → 'INSERT 0 0' (гонка с параллельным прогоном, страховка
        # partial unique index поверх cooldown-SELECT). Реальная вставка → 'INSERT 0 1'.
        if status.endswith(" 0"):
            skipped += 1
        else:
            snapshot = await _snapshot_for(p)
            has_l2 = await _create_l2_action(p, pid, snapshot) is not None
            written.append((p, pid, has_l2))
    return written, skipped


def _action_for_verdict(proposal: Proposal, current_cap: float | None = None) -> tuple[str, dict] | None:
    """Вердикт → (kind, payload) L2-действия, либо None если действия нет.

    CLOSE_POSITION_REVIEW маппится в pause_detector, НЕ в close_position: вердикт
    говорит «останови детектор», а закрытие требует конкретный order_db_id, которого
    у policy engine нет — он агрегирует по детектору. Закрытие живёт в контуре close_pos:.
    """
    if proposal.scope.startswith("policy:"):
        return None
    detector = proposal.scope.removeprefix("detector:").split(":category:")[0]

    if proposal.verdict in ("PAUSE_REVIEW", "CLOSE_POSITION_REVIEW"):
        return KIND_PAUSE_DETECTOR, {"detector": detector, "reason": f"advisory L2: {proposal.verdict}"}

    if proposal.verdict == "REDUCE_RISK_REVIEW":
        if current_cap is None or current_cap <= 0:
            # Не выдумываем число: без известного текущего cap «урезать вдвое» неоткуда.
            return None
        return KIND_REDUCE_CAP, {"detector": detector, "new_cap_usd": round(current_cap / 2, 2)}

    return None  # KEEP_SHADOW / INVESTIGATE_DATA / OBSERVE — L2-действия нет


async def _snapshot_for(proposal: Proposal) -> dict:
    """Слепок медленно меняющихся величин детектора на момент отправки (спека §4).

    Цена и время сюда НЕ попадают намеренно: снапшот сверяется при клике, и
    волатильное поле давало бы отказ почти всегда (спека §8 риск 2).
    """
    if proposal.scope.startswith("policy:"):
        return {}
    detector = proposal.scope.removeprefix("detector:").split(":category:")[0]
    row = await fetchrow(_L2_SNAPSHOT_SQL, detector)
    if row is None:
        return {}
    return {"stage": row["stage"], "live_stake_cap_usd": row["live_stake_cap_usd"]}


async def _create_l2_action(proposal: Proposal, pid: str, snapshot: dict) -> str | None:
    """Завести L2-proposal в agent_actions. Возвращает pid как action_id, либо None.

    snapshot — слепок величин, по которым принято решение (§4 спеки). Его хэш
    сверяется при клике: если состояние изменилось, действие не выполняется.
    """
    mapped = _action_for_verdict(proposal, current_cap=snapshot.get("live_stake_cap_usd"))
    if mapped is None:
        return None
    kind, payload = mapped
    await execute(
        _INSERT_L2_ACTION_SQL,
        kind,
        json.dumps(payload, sort_keys=True),
        snapshot_hash(snapshot),
        pid,
    )
    return pid


async def _send_advisory(
    written: list[tuple[Proposal, str, bool]], dq_verdict: str, ips_version: str | None, enabled: bool
) -> tuple[int, int]:
    """Шлёт advisory с L1-кнопками по записанным proposals (спека §5, C3b §3.4).

    Кнопки прикрепляются только к should_send-proposal. Рассылка каждому админу
    (send_telegram_with_keyboard_rows шлёт одному chat_id). Возвращает (sent, failures):
    failures — 5xx/HTTPError от TG (тихая потеря advisory наблюдаема, C3a known-gap).
    """
    from app.analytics.advisory_buttons import build_advisory_keyboard
    from app.api.telegram import send_telegram_with_keyboard_rows

    admin_ids = get_settings().admin_telegram_ids
    sent = 0
    failures = 0
    # has_l2 — есть ли парная L2-строка в agent_actions; кнопка «⚡ Сделать» появится
    # только тогда, иначе клик вёл бы в никуда (нет строки под pid для _handle_action_callback).
    for p, pid, has_l2 in written:
        if not should_send(p, enabled):
            continue
        text = render_proposal(p, dq_verdict, ips_version)
        if text is None:
            continue
        rows = build_advisory_keyboard(pid, with_execute=has_l2)
        for admin_id in admin_ids:
            ok = await send_telegram_with_keyboard_rows(admin_id, text, rows)
            if ok:
                sent += 1
            else:
                failures += 1
    return sent, failures


async def _persist_blocked_claims(proposals: list[Proposal], dq_run_id: int | None) -> int:
    """DQ RED / IPS violation: blocked risk-вердикты → BLOCKED_DATA-клеймы (спека §4, §2 п.8), не proposals."""
    n = 0
    evidence = [dq_run_id] if dq_run_id is not None else []
    for p in proposals:
        if not p.blocked:
            continue
        statement = f"policy verdict blocked by {p.blocked_by or 'DQ_RED'}: {p.verdict} {p.scope}"
        await execute(_BLOCKED_CLAIM_SQL, statement, p.scope, evidence)
        n += 1
    return n


async def _persist_blocked_cycle_marker(n_blocked: int, rule_version: int, cause: str, ips_version: str | None) -> None:
    """Спека §5: DQ RED/IPS violation в момент цикла → «risk-вердикты не эмитятся; OBSERVE-run с флагом blocked».

    Вызывать только когда risk-вердикты реально были подавлены (n_blocked>0) — при DQ RED и
    отсутствии risk-вердиктов evaluate() уже выдаёт обычный INVESTIGATE_DATA-proposal, отдельный
    маркер не нужен. Отдельный namespace хэша ('OBSERVE_BLOCKED'), чтобы не конфликтовать с
    обычным OBSERVE-proposal_id того же окна.
    """
    window = datetime.now(UTC).strftime("%Y-%m-%d")
    pid = proposal_id("OBSERVE_BLOCKED", "portfolio:all", rule_version, window)
    metrics = json.dumps(
        {
            "reason": f"cycle blocked by {cause}: {n_blocked} risk verdicts suppressed",
            "blocked": True,
            "suppressed_count": n_blocked,
            "rule_version": rule_version,
            "shadow": True,
            "ips_version": ips_version,
        }
    )
    await execute(
        _INSERT_PROPOSAL_SQL,
        "policy_engine:portfolio:all",
        pid,
        f"rule_v{rule_version}",
        "policy_engine_daily_v1",
        metrics,
        VERDICT_OBSERVE,
    )


async def run_policy_engine_daily() -> dict:
    """Полный цикл C1+C2+C3a: load rule → fetch → IPS-гейт → evaluate → persist → advisory-отправка."""
    enabled = get_settings().policy_engine_enabled
    rule_version, thresholds = await _load_rule()
    dq = await _fetch_dq_status()
    stages = await _fetch_detector_stages()
    signals = await _fetch_monitor_signals(stages)
    drawdowns = await _fetch_live_drawdowns(thresholds.drawdown_window_days)
    ips = await _load_ips_gate(drawdowns, thresholds.drawdown_window_days, dq.verdict)

    proposals = evaluate(dq, signals, drawdowns, thresholds, rule_version, ips_gate=ips)

    written, skipped = await _persist_proposals(proposals, thresholds.cooldown_hours, ips.version_hash, enabled)
    blocked = await _persist_blocked_claims(proposals, dq.run_id)

    if blocked > 0 and (dq.verdict == "RED" or ips.violated):
        cause = "DQ RED" if dq.verdict == "RED" else "IPS_VIOLATION"
        await _persist_blocked_cycle_marker(blocked, rule_version, cause, ips.version_hash)

    sent, send_failures = await _send_advisory(written, dq.verdict, ips.version_hash, enabled)

    summary = {
        "proposals_written": len(written),
        "skipped_cooldown": skipped,
        "blocked_claims": blocked,
        "risk_blocked": dq.verdict == "RED" or ips.violated,  # спека §5: маркер заблокированного цикла
        "dq_verdict": dq.verdict,
        "rule_version": rule_version,
        "ips_gate_active": ips.active,
        "ips_violated": ips.violated,
        "ips_version": ips.version_hash,
        "advisory_sent": sent,
        "advisory_send_failures": send_failures,
        "policy_engine_enabled": enabled,
    }
    logger.info("policy_engine_daily_done %s", summary)
    return summary
