"""Golden-сценарии Phase 2 C5 — реальные инциденты как замороженные фикстуры.

Спека: docs/superpowers/specs/2026-07-29-phase2-c5-golden-harness-design.md.
Состав: 5 инцидентов §6 родительской спеки + сценарий 06, добавленный 2026-07-30
ради покрытия правила adverse_selection (единственного правила §4, которое
5 исходных сценариев не активировали — гэп найден мутационным тестом).

ВАЖНО: снапшоты сообщений в tests/golden/messages/*.txt правятся ТОЛЬКО руками.
Автогенерации нет намеренно (спека §2 п.5): "тест покраснел → регенерировал →
закоммитил" превращает harness в фикцию.

Поле `source` обязательно у каждого сценария и различает прод-слепок от
реконструкции по постмортему — golden-тест, выдающий реконструкцию за
исторические данные, создаёт ложную уверенность (спека §2 п.3).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.analytics.dq_audit import (
    DqCheckResult,
    DqThresholds,
    classify_expired_share,
    classify_freshness,
    classify_share,
    overall_verdict,
)
from app.analytics.policy_engine import (
    DqCheckLine,
    DqStatus,
    LiveDrawdown,
    MonitorSignal,
    PolicyThresholds,
)

# Пороги v1 — прод policy_rules version=1 (сверяются с _DEFAULT_PARAMS в задаче 6).
# cooldown_hours поднят 6 → 25 (policy_rules version=2, 2026-08-01): на дедуп-вердикты
# и тексты сообщений НЕ влияет — evaluate() его не читает, он используется только
# в _persist_proposals. Поэтому expected сценариев и снапшоты не пересматривались.
THRESHOLDS_V1 = PolicyThresholds(
    drawdown_reduce_roi=-0.15,
    drawdown_close_roi=-0.30,
    drawdown_min_settled=20,
    drawdown_window_days=30,
    cooldown_hours=25,
)

RULE_VERSION_V1 = 1

# Пороги DQ v1 — прод policy_rules rule_key='dq_audit' version=1 (снято 2026-07-29).
DQ_THRESHOLDS_V1 = DqThresholds.from_params(
    {
        "freshness_markets_amber_minutes": 120.0,
        "freshness_markets_red_minutes": 720.0,
        "freshness_signals_amber_minutes": 360.0,
        "freshness_signals_red_minutes": 1440.0,
        "completeness_end_date_amber_pct": 15.0,
        "completeness_end_date_red_pct": 40.0,
        "completeness_tokens_amber_pct": 5.0,
        "completeness_tokens_red_pct": 20.0,
        "expired_share_amber_pct": 30.0,
        "expired_share_red_pct": 60.0,
        "expired_min_resolved": 50,
        "ri_orphan_amber_pct": 1.0,
        "ri_orphan_red_pct": 5.0,
        "lookback_days": 7,
    }
)

# Явный GREEN-чек для сценариев без DQ-инцидента (04-05).
# Пустой кортеж дал бы RED: overall_verdict([]) — fail-closed (dq_audit.py:142).
GREEN_CHECK = DqCheckResult(
    check="freshness:markets",
    verdict="GREEN",
    measured=5.0,
    amber_threshold=120.0,
    red_threshold=720.0,
    detail="markets: возраст последней записи 5 мин",
)


@dataclass(frozen=True)
class GoldenScenario:
    """Один замороженный инцидент-реплей."""

    key: str  # он же имя файла снапшота: tests/golden/messages/<key>.txt
    title: str
    source: str  # "prod-query YYYY-MM-DD" | "reconstructed: <постмортем>"
    dq_checks: tuple[DqCheckResult, ...]
    dq_run_id: int | None
    signals: tuple[MonitorSignal, ...] = field(default_factory=tuple)
    drawdowns: tuple[LiveDrawdown, ...] = field(default_factory=tuple)
    thresholds: PolicyThresholds = THRESHOLDS_V1
    rule_version: int = RULE_VERSION_V1
    expected: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    required_slots: tuple[str, ...] = field(default_factory=tuple)
    expected_blocked: tuple[str, ...] = field(default_factory=tuple)  # scope'ы, заблокированные DQ RED
    ips_version: str | None = None


def build_dq_status(scenario: GoldenScenario) -> DqStatus:
    """Единственное правило вывода DqStatus (спека §3) — без исключений."""
    return DqStatus(
        verdict=overall_verdict(list(scenario.dq_checks)),
        run_id=scenario.dq_run_id,
        checks=tuple(DqCheckLine(check=c.check, verdict=c.verdict, detail=c.detail) for c in scenario.dq_checks),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 05 — live drawdown D3 (2026-07-21). ЕДИНСТВЕННЫЙ сценарий на живых прод-числах.
#
# Снято 2026-07-29 тем же SQL, что _DRAWDOWN_SQL (policy_engine_daily.py:79-90):
#   D3_cross_mkt_arb | 39 settled | net_pnl -80.7962 | stake 242.2600 | ROI -0.333510
# Окно "30д от сегодня" и "30д от даты инцидента" дали идентичный результат —
# новых settled с инцидента нет, фикстура стабильна.
#
# ROI -33.35% < drawdown_close_roi=-0.30 → severe-порог → CLOSE_POSITION_REVIEW.
#
# АСИММЕТРИЯ, которую сценарий защищает: D3 стоит в detector_state.stage='virtual',
# но drawdown считается по фактическим live-ордерам — by design (_DRAWDOWN_SQL:77-78).
# Без этого реплея будущая "оптимизация", скоупящая drawdown по detector_state.stage,
# прошла бы мимо тестов.
# ─────────────────────────────────────────────────────────────────────────────
SCENARIO_05_LIVE_DRAWDOWN = GoldenScenario(
    key="05_live_drawdown_d3",
    title="D3 live drawdown во всех категориях (2026-07-21)",
    source="prod-query 2026-07-29",
    dq_checks=(GREEN_CHECK,),
    dq_run_id=1001,
    drawdowns=(
        LiveDrawdown(
            detector="D3_cross_mkt_arb",
            n_settled=39,
            net_pnl=-80.7962,
            stake=242.2600,
        ),
    ),
    expected=(("CLOSE_POSITION_REVIEW", "detector:D3_cross_mkt_arb"),),
    required_slots=(
        "D3 — арбитраж между рынками",  # человеческое имя, не сырой id
        "-33.4%",
        "39 сделок",
        "$242.26",
        "Что делать:",  # блок действия обязателен (спека 2026-08-01 §3)
        "Если ничего не делать:",
    ),
)

# ─────────────────────────────────────────────────────────────────────────────
# 01 — stale book (WS cache TTL-eviction 2026-07-03 / stale-cap PR #724).
#
# Реконструкция: мониторы Phase 1 и C0 не существовали на дату инцидента,
# agent_runs(kind='dq') за 2026-07-03 в проде нет.
#
# Свежесть detector_signals 400 мин при прод-порогах amber=360 / red=1440 → AMBER.
# ГРАНИЦА: _classify_value использует >= (fail-closed, dq_audit.py:69-75), т.е.
# ровно 360.0 дало бы AMBER, а 359.9 — GREEN. Взято 400 с запасом от границы.
#
# AMBER (в отличие от RED) risk-вердикты НЕ блокирует — это и проверяется:
# edge_decay-сигнал доходит до PAUSE_REVIEW неблокированным.
# ─────────────────────────────────────────────────────────────────────────────
SCENARIO_01_STALE_BOOK = GoldenScenario(
    key="01_stale_book",
    title="Stale book — протухшие снапшоты цен (2026-07-03)",
    source="reconstructed: WS cache TTL-eviction 2026-07-03 / stale-cap PR #724",
    dq_checks=(classify_freshness(400.0, 360.0, 1440.0, "detector_signals"),),
    dq_run_id=2001,
    signals=(
        MonitorSignal(
            monitor="edge_decay",
            entity="D38_resolution_lag",
            category=None,
            verdict="decaying",
            run_id=3001,
            stage="virtual",
        ),
    ),
    expected=(
        ("INVESTIGATE_DATA", "policy:dq_audit"),
        ("PAUSE_REVIEW", "detector:D38_resolution_lag"),
    ),
    required_slots=(
        "Качество данных",
        "под вопросом",  # AMBER словами
        "run #2001",
        "D38 — задержка резолва",
        "Что делать:",
    ),
)

# ─────────────────────────────────────────────────────────────────────────────
# 02 — ID mismatch (M2/D45 market_id UUID 2026-07-09, PR #683/#685).
#
# Реконструкция: C0-аудита на дату инцидента не было.
# Симптом инцидента — detector_signals.market_id содержал внутренний UUID
# markets.id вместо polymarket_id (0x), из-за чего resolve-JOIN не находил
# рынок → сироты в referential integrity.
#
# 12% сирот при прод-порогах amber=1 / red=5 → RED.
# RED блокирует risk-вердикты: edge_decay-сигнал становится blocked=True
# (blocked_by='DQ_RED') и render_proposal возвращает для него None —
# он живёт как BLOCKED_DATA-клейм, а не как сообщение.
# ─────────────────────────────────────────────────────────────────────────────
SCENARIO_02_ID_MISMATCH = GoldenScenario(
    key="02_id_mismatch",
    title="ID mismatch — UUID вместо polymarket_id (2026-07-09)",
    source="reconstructed: M2/D45 market_id UUID 2026-07-09 (PR #683/#685)",
    dq_checks=(
        classify_share(
            "ri:orphans",
            12.0,
            1.0,
            5.0,
            "detector_signals.market_id без markets: 12/100 = 12.0%",
        ),
    ),
    dq_run_id=2002,
    signals=(
        MonitorSignal(
            monitor="edge_decay",
            entity="M2_liquidity_vacuum",
            category=None,
            verdict="decaying",
            run_id=3002,
            stage="shadow",
        ),
    ),
    expected=(
        ("INVESTIGATE_DATA", "policy:dq_audit"),
        ("PAUSE_REVIEW", "detector:M2_liquidity_vacuum"),
    ),
    expected_blocked=("detector:M2_liquidity_vacuum",),
    required_slots=(
        "Качество данных",
        "ненадёжны",  # RED словами
        "run #2002",
        "Что делать:",
    ),
)

# ─────────────────────────────────────────────────────────────────────────────
# 03 — EXPIRED-спайк (100% EXPIRED = маркер ID-бага, семья инцидентов M2/D45).
#
# Реконструкция: C0-аудита на дату инцидента не было.
#
# ЛОВУШКА (проверено чтением dq_audit.py:120-128): при resolved_total <
# expired_min_resolved (прод-порог 50) функция возвращает GREEN независимо
# от доли EXPIRED — "не наказываем тихие периоды". Фикстура 10/10 молча
# выродилась бы в GREEN, и тест проверял бы не то, что заявлено в названии.
# Взято 60/60 → 100% → RED.
#
# Risk-сигналов в сценарии нет намеренно: проверяется чистый DQ-путь.
# ─────────────────────────────────────────────────────────────────────────────
SCENARIO_03_EXPIRED_SPIKE = GoldenScenario(
    key="03_expired_spike",
    title="EXPIRED-спайк — 100% резолвов EXPIRED (маркер ID-бага)",
    source="reconstructed: 100% EXPIRED-маркер, семья инцидентов M2/D45 2026-07-09",
    dq_checks=(classify_expired_share(60, 60, DQ_THRESHOLDS_V1),),
    dq_run_id=2003,
    expected=(("INVESTIGATE_DATA", "policy:dq_audit"),),
    required_slots=(
        "Качество данных",
        "ненадёжны",
        "run #2003",
        "Что делать:",
    ),
)

# ─────────────────────────────────────────────────────────────────────────────
# 04 — drift (D42 demote 2026-07-16, семантика sum_roi).
#
# Реконструкция: edge_decay-монитор на дату инцидента ещё не писал agent_runs.
#
# Суть инцидента: демоут D42 показал, что сумма per-signal ROI ≠ портфельный
# результат; policy engine работает поверх bankroll-weighted семантики (§4 родителя).
# Здесь монитор уже вынес verdict='decaying' — проверяется, что stage='shadow'
# (не live) даёт консервативный PAUSE_REVIEW, а не REDUCE_RISK_REVIEW
# (policy_engine.py:132-137).
#
# DQ GREEN намеренно: изолируем drift-путь от DQ-путей сценариев 01-03.
# ─────────────────────────────────────────────────────────────────────────────
SCENARIO_04_DRIFT_D42 = GoldenScenario(
    key="04_drift_d42",
    title="Drift — D42 demote, семантика sum_roi (2026-07-16)",
    source="reconstructed: D42 demote 2026-07-16 (sum_roi семантика)",
    dq_checks=(GREEN_CHECK,),
    dq_run_id=2004,
    signals=(
        MonitorSignal(
            monitor="edge_decay",
            entity="D42_event_driven_ml",
            category=None,
            verdict="decaying",
            run_id=3004,
            stage="shadow",
        ),
    ),
    expected=(("PAUSE_REVIEW", "detector:D42_event_driven_ml"),),
    required_slots=(
        "D42 — событийная ML-модель",
        "Точность предсказаний просела",
        "«тень»",  # stage словами
        "run #3004",
        "Что делать:",
    ),
)

# ─────────────────────────────────────────────────────────────────────────────
# 06 — adverse selection в temperature-категории (2026-07-21).
#
# SOURCE СМЕШАННЫЙ, и поэтому помечен слабейшим из двух (спека §2 п.3:
# выдавать реконструкцию за прод-слепок — ровно та ложная уверенность,
# против которой строится C5). Первый сигнал — живой прод-слепок, второй —
# реконструкция; поле `source` одно на сценарий, так что взято 'reconstructed'.
#
# Сигнал 1 (ПРОД). Снято 2026-07-30 из agent_runs(kind='monitoring'),
# run #47 — приёмочный прогон §9 монитора Компонента 3 Phase 1 (PR #719):
#   name    adverse_selection_monitor:STANDALONE_V2_LGB×World & Economy
#   verdict adverse
#   metrics n_events=20, wins=6 (wr 30%), wilson_lower=0.1455, net_pnl=-15973.95
# Это эталонный temperature-якорь: метео-рынки World & Economy, где по ту
# сторону книги стоят информированные метео-боты.
#
# ПОКРЫВАЕТ ОБЕ ВЕТКИ правила (policy_engine.py:147-167) одним сценарием —
# гэп §6, найденный мутационным тестом на whole-branch ревью 2026-07-29:
#   • STANDALONE_V2_LGB (прод-stage 'paused', сверено 2026-07-30) → НЕ live
#     → KEEP_SHADOW, scope БЕЗ категории (блок promote детектора целиком);
#   • D3_cross_mkt_arb stage='live' → REDUCE_RISK_REVIEW, scope category-scoped.
# Асимметрия scope'ов намеренная и она же — предмет заморозки: KEEP_SHADOW
# блокирует promote всего детектора, REDUCE_RISK_REVIEW сужает риск до
# конкретной категории. Их подмена местами прошла бы мимо остальных 5 сценариев.
#
# Сигнал 2 (РЕКОНСТРУКЦИЯ). У D3 нет adverse-строки в agent_runs: монитор
# считает virtual-only, а D3 на погоде торговал живыми деньгами — 14 сделок,
# wr 35.7%, net -$28.88, окно 30.06→20.07 (постмортем 2026-07-21). Тот же
# инцидент, вторая сторона: именно D3 доказал реальными деньгами то, что
# D38/STANDALONE показывали в виртуале. run_id 3006 — синтетический, как
# у реконструированных сценариев 01-04.
#
# DQ GREEN намеренно: изолируем adverse-путь от DQ-путей сценариев 01-03.
# ─────────────────────────────────────────────────────────────────────────────
SCENARIO_06_ADVERSE_SELECTION = GoldenScenario(
    key="06_adverse_selection",
    title="Adverse selection — temperature-якорь World & Economy (2026-07-21)",
    source=(
        "reconstructed: adverse selection на temperature 2026-07-21 (PR #719); "
        "сигнал STANDALONE_V2_LGB — прод-слепок agent_runs run #47 от 2026-07-30, "
        "сигнал D3×Temperature реконструирован по постмортему"
    ),
    dq_checks=(GREEN_CHECK,),
    dq_run_id=2006,
    signals=(
        MonitorSignal(
            monitor="adverse_selection",
            entity="STANDALONE_V2_LGB",
            category="World & Economy",
            verdict="adverse",
            run_id=47,
            stage="paused",
        ),
        MonitorSignal(
            monitor="adverse_selection",
            entity="D3_cross_mkt_arb",
            category="Temperature",
            verdict="adverse",
            run_id=3006,
            stage="live",
        ),
    ),
    expected=(
        ("KEEP_SHADOW", "detector:STANDALONE_V2_LGB"),
        ("REDUCE_RISK_REVIEW", "detector:D3_cross_mkt_arb:category:Temperature"),
    ),
    required_slots=(
        "STANDALONE_V2_LGB — основная ML-модель",
        "World &amp; Economy",  # html.escape в render_proposal
        "его переигрывают именно там",
        "run #47",
        "D3 — арбитраж между рынками",
        "Temperature",
        "Что делать:",
    ),
)

ALL_SCENARIOS: tuple[GoldenScenario, ...] = (
    SCENARIO_01_STALE_BOOK,
    SCENARIO_02_ID_MISMATCH,
    SCENARIO_03_EXPIRED_SPIKE,
    SCENARIO_04_DRIFT_D42,
    SCENARIO_05_LIVE_DRAWDOWN,
    SCENARIO_06_ADVERSE_SELECTION,
)
