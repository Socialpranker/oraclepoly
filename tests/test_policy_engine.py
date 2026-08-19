"""Тесты чистого слоя policy engine (Phase 2 C1)."""

from app.analytics.ips import INACTIVE_IPS_GATE, IpsGate
from app.analytics.policy_engine import (
    _DEFAULT_PARAMS,
    RISK_VERDICTS,
    VERDICT_CLOSE_POSITION_REVIEW,
    VERDICT_INVESTIGATE_DATA,
    VERDICT_KEEP_SHADOW,
    VERDICT_LIVE_RAMP_REVIEW,
    VERDICT_OBSERVE,
    VERDICT_PAUSE_REVIEW,
    VERDICT_REDUCE_RISK_REVIEW,
    VERDICT_START_VIRTUAL_REVIEW,
    VERDICTS,
    DqCheckLine,
    DqStatus,
    LiveDrawdown,
    MonitorSignal,
    PolicyThresholds,
    evaluate,
)


def test_verdict_enum_has_exactly_eight_values():
    assert VERDICTS == frozenset(
        {
            VERDICT_OBSERVE,
            VERDICT_INVESTIGATE_DATA,
            VERDICT_KEEP_SHADOW,
            VERDICT_PAUSE_REVIEW,
            VERDICT_REDUCE_RISK_REVIEW,
            VERDICT_CLOSE_POSITION_REVIEW,
            VERDICT_START_VIRTUAL_REVIEW,
            VERDICT_LIVE_RAMP_REVIEW,
        }
    )
    assert len(VERDICTS) == 8


def test_thresholds_from_params_roundtrip():
    t = PolicyThresholds.from_params(_DEFAULT_PARAMS)
    assert t.drawdown_reduce_roi == -0.15
    assert t.drawdown_close_roi == -0.30
    assert t.drawdown_min_settled == 20
    assert t.drawdown_window_days == 30
    assert t.cooldown_hours == 25


def test_cooldown_window_strictly_exceeds_daily_job_interval():
    """Дедуп advisory работает, только если окно cooldown ПЕРЕКРЫВАЕТ интервал прогонов.

    policy-engine — суточный cron (07:30), фактический интервал ровно 24.00ч.
    Окно 24ч попадает на границу и срабатывает через раз по джиттеру планировщика
    (контрфактика на 17 исторических proposals: 24ч подавляет лишь 8/17).
    Окно < 24ч не подавляет ничего вовсе — это и был баг 2026-08-01 (6ч → 0/17).
    Тест держит порог строго выше суток, чтобы «упрощение» до 24 не вернуло флап.
    """
    t = PolicyThresholds.from_params(_DEFAULT_PARAMS)
    assert t.cooldown_hours > 24


def test_live_drawdown_roi():
    dd = LiveDrawdown(detector="D3_cross_mkt_arb", n_settled=39, net_pnl=-80.80, stake=242.26)
    assert dd.roi is not None
    assert abs(dd.roi - (-80.80 / 242.26)) < 1e-9
    assert LiveDrawdown(detector="x", n_settled=0, net_pnl=0.0, stake=0.0).roi is None


_T = PolicyThresholds.from_params(_DEFAULT_PARAMS)
_GREEN = DqStatus(verdict="GREEN", run_id=100)


def _sig(monitor, entity, verdict, stage, category=None, run_id=1):
    return MonitorSignal(monitor=monitor, entity=entity, category=category, verdict=verdict, run_id=run_id, stage=stage)


def test_all_clean_yields_single_observe():
    props = evaluate(_GREEN, [], [], _T, rule_version=1)
    assert len(props) == 1
    assert props[0].verdict == VERDICT_OBSERVE
    assert props[0].scope == "portfolio:all"
    assert props[0].blocked is False


def test_dq_amber_yields_investigate_data():
    props = evaluate(DqStatus("AMBER", 101), [], [], _T, rule_version=1)
    verdicts = {p.verdict for p in props}
    assert VERDICT_INVESTIGATE_DATA in verdicts
    inv = next(p for p in props if p.verdict == VERDICT_INVESTIGATE_DATA)
    assert inv.scope == "policy:dq_audit"
    assert 101 in inv.evidence_run_ids


def test_edge_decay_virtual_yields_pause_review():
    sig = _sig("edge_decay", "D38_resolution_lag", "decaying", stage="virtual", run_id=7)
    props = evaluate(_GREEN, [sig], [], _T, rule_version=1)
    pause = next(p for p in props if p.verdict == VERDICT_PAUSE_REVIEW)
    assert pause.scope == "detector:D38_resolution_lag"
    assert 7 in pause.evidence_run_ids


def test_edge_decay_live_yields_reduce_risk():
    sig = _sig("edge_decay", "D3_cross_mkt_arb", "decaying", stage="live", run_id=8)
    props = evaluate(_GREEN, [sig], [], _T, rule_version=1)
    assert any(p.verdict == VERDICT_REDUCE_RISK_REVIEW and p.scope == "detector:D3_cross_mkt_arb" for p in props)


def test_edge_decay_stable_is_ignored():
    sig = _sig("edge_decay", "D3_cross_mkt_arb", "stable", stage="virtual", run_id=9)
    props = evaluate(_GREEN, [sig], [], _T, rule_version=1)
    assert [p.verdict for p in props] == [VERDICT_OBSERVE]


def test_adverse_selection_virtual_yields_keep_shadow():
    sig = _sig("adverse_selection", "D36_obi_pressure", "adverse", stage="shadow", category="Sports", run_id=10)
    props = evaluate(_GREEN, [sig], [], _T, rule_version=1)
    keep = next(p for p in props if p.verdict == VERDICT_KEEP_SHADOW)
    assert keep.scope == "detector:D36_obi_pressure"


def test_adverse_selection_live_yields_reduce_risk_category_scoped():
    sig = _sig("adverse_selection", "D3_cross_mkt_arb", "adverse", stage="live", category="Crypto", run_id=11)
    props = evaluate(_GREEN, [sig], [], _T, rule_version=1)
    red = next(p for p in props if p.verdict == VERDICT_REDUCE_RISK_REVIEW)
    assert red.scope == "detector:D3_cross_mkt_arb:category:Crypto"


def test_live_drawdown_reduce_and_close_thresholds():
    mild = LiveDrawdown("Dmild", n_settled=25, net_pnl=-20.0, stake=100.0)  # −20%
    severe = LiveDrawdown("D3_cross_mkt_arb", n_settled=39, net_pnl=-80.80, stake=242.26)  # −33%
    props = evaluate(_GREEN, [], [mild, severe], _T, rule_version=1)
    by_scope = {p.scope: p.verdict for p in props if p.verdict != VERDICT_OBSERVE}
    assert by_scope["detector:Dmild"] == VERDICT_REDUCE_RISK_REVIEW
    assert by_scope["detector:D3_cross_mkt_arb"] == VERDICT_CLOSE_POSITION_REVIEW


def test_live_drawdown_below_min_n_is_ignored():
    dd = LiveDrawdown("Dtiny", n_settled=5, net_pnl=-50.0, stake=100.0)  # −50%, но n<20
    props = evaluate(_GREEN, [], [dd], _T, rule_version=1)
    assert [p.verdict for p in props] == [VERDICT_OBSERVE]


def test_live_drawdown_exact_boundary_uses_strict_less_than():
    """Пороги в _drawdown_proposals — строгое `<` (не `<=`): roi ровно на границе НЕ триггерит
    более жёсткий вердикт следующего уровня, а roi ровно на reduce-границе не триггерит вообще."""
    # roi == -0.30 ровно (close-порог) → strict `<` не срабатывает для CLOSE, но всё ещё
    # < reduce-порога (-0.15) → REDUCE_RISK_REVIEW, не CLOSE_POSITION_REVIEW
    at_close_boundary = LiveDrawdown("D_close_edge", n_settled=25, net_pnl=-30.0, stake=100.0)  # roi == -0.30
    # roi == -0.15 ровно (reduce-порог) → strict `<` не срабатывает → нет risk-вердикта, только OBSERVE
    at_reduce_boundary = LiveDrawdown("D_reduce_edge", n_settled=25, net_pnl=-15.0, stake=100.0)  # roi == -0.15

    props_close = evaluate(_GREEN, [], [at_close_boundary], _T, rule_version=1)
    by_scope_close = {p.scope: p.verdict for p in props_close if p.verdict != VERDICT_OBSERVE}
    assert by_scope_close["detector:D_close_edge"] == VERDICT_REDUCE_RISK_REVIEW

    props_reduce = evaluate(_GREEN, [], [at_reduce_boundary], _T, rule_version=1)
    assert [p.verdict for p in props_reduce] == [VERDICT_OBSERVE]


def test_dq_red_blocks_risk_verdicts():
    sig = _sig("edge_decay", "D38_resolution_lag", "decaying", stage="virtual", run_id=12)
    dd = LiveDrawdown("D3_cross_mkt_arb", n_settled=39, net_pnl=-80.80, stake=242.26)
    props = evaluate(DqStatus("RED", 102), [sig], [dd], _T, rule_version=1)
    # INVESTIGATE_DATA эмитится нормально
    assert any(p.verdict == VERDICT_INVESTIGATE_DATA and not p.blocked for p in props)
    # risk-вердикты присутствуют, но помечены blocked=True (для BLOCKED_DATA-клеймов в джобе)
    risk = [p for p in props if p.verdict in {VERDICT_PAUSE_REVIEW, VERDICT_CLOSE_POSITION_REVIEW}]
    assert risk and all(p.blocked for p in risk)


def test_all_verdicts_from_enum():
    sigs = [
        _sig("edge_decay", "A", "decaying", "virtual", run_id=1),
        _sig("adverse_selection", "B", "adverse", "live", category="Sports", run_id=2),
    ]
    dd = [LiveDrawdown("C", 30, -40.0, 100.0)]
    for dq_verdict in ("GREEN", "AMBER", "RED"):
        for p in evaluate(DqStatus(dq_verdict, 1), sigs, dd, _T, rule_version=1):
            assert p.verdict in VERDICTS


def test_evaluate_ips_violation_blocks_risk_verdicts():
    dq = DqStatus(verdict="GREEN", run_id=1)
    dd = [LiveDrawdown(detector="D3", n_settled=30, net_pnl=-40.0, stake=100.0)]  # ROI −40% → CLOSE
    gate = IpsGate(
        active=True,
        violated=True,
        violations=("forbidden category exposure: politics ($12.50)",),
        version_hash="abc",
    )
    thresholds = PolicyThresholds.from_params(_DEFAULT_PARAMS)
    proposals = evaluate(dq, [], dd, thresholds, rule_version=1, ips_gate=gate)
    risk = [p for p in proposals if p.verdict in RISK_VERDICTS]
    assert risk and all(p.blocked and p.blocked_by == "IPS_VIOLATION" for p in risk)
    inv = [p for p in proposals if p.verdict == VERDICT_INVESTIGATE_DATA and p.scope == "policy:ips"]
    assert len(inv) == 1 and "politics" in inv[0].reason


def test_evaluate_dq_red_takes_precedence_over_ips():
    dq = DqStatus(verdict="RED", run_id=1)
    dd = [LiveDrawdown(detector="D3", n_settled=30, net_pnl=-40.0, stake=100.0)]
    gate = IpsGate(active=True, violated=True, violations=("x",), version_hash="abc")
    thresholds = PolicyThresholds.from_params(_DEFAULT_PARAMS)
    proposals = evaluate(dq, [], dd, thresholds, rule_version=1, ips_gate=gate)
    risk = [p for p in proposals if p.verdict in RISK_VERDICTS]
    assert risk and all(p.blocked_by == "DQ_RED" for p in risk)


def test_evaluate_inactive_gate_changes_nothing():
    dq = DqStatus(verdict="GREEN", run_id=1)
    thresholds = PolicyThresholds.from_params(_DEFAULT_PARAMS)
    assert evaluate(dq, [], [], thresholds, 1, ips_gate=INACTIVE_IPS_GATE) == evaluate(dq, [], [], thresholds, 1)


def _line(check, verdict, detail):
    return DqCheckLine(check=check, verdict=verdict, detail=detail)


def _dq_reason(dq):
    """reason INVESTIGATE_DATA-предложения из evaluate() — тесты идут через публичный API."""
    props = evaluate(dq, [], [], _T, rule_version=1)
    return next(p for p in props if p.verdict == VERDICT_INVESTIGATE_DATA).reason


def test_dq_reason_names_single_failed_check():
    # Реплей прод-run #257 (2026-07-26): freshness:markets RED, 2713 мин.
    dq = DqStatus(
        verdict="RED",
        run_id=257,
        checks=(_line("freshness:markets", "RED", "markets: возраст последней записи 2713 мин"),),
    )
    reason = _dq_reason(dq)
    assert "freshness:markets" in reason
    assert "2713" in reason
    assert "RED" in reason


def test_dq_reason_lists_every_non_green_check():
    dq = DqStatus(
        verdict="RED",
        run_id=300,
        checks=(
            _line("freshness:markets", "RED", "markets: возраст последней записи 2713 мин"),
            _line("completeness:end_date", "AMBER", "без end_date 8000/36319 = 22.0%"),
        ),
    )
    reason = _dq_reason(dq)
    assert "freshness:markets" in reason
    assert "completeness:end_date" in reason
    assert "22.0%" in reason
    assert reason.count("\n•") == 2


def test_dq_reason_omits_green_checks():
    dq = DqStatus(
        verdict="AMBER",
        run_id=301,
        checks=(
            _line("freshness:markets", "GREEN", "markets: возраст последней записи 13 мин"),
            _line("expired_share", "AMBER", "EXPIRED 40/60 = 66.7%"),
        ),
    )
    reason = _dq_reason(dq)
    assert "expired_share" in reason
    assert "freshness:markets" not in reason
    assert reason.count("\n•") == 1


def test_dq_reason_does_not_leak_thresholds():
    # Спека §3 п.2: пороги живут в 🟡-алерте C0, в advisory их нет.
    dq = DqStatus(
        verdict="RED",
        run_id=257,
        checks=(_line("freshness:markets", "RED", "markets: возраст последней записи 2713 мин"),),
    )
    reason = _dq_reason(dq)
    assert "amber" not in reason.lower()
    assert "red≥" not in reason


def test_dq_reason_fallback_when_run_missing():
    # _fetch_dq_status fail-closed: run'а нет → run_id=None. Это другой инцидент.
    reason = _dq_reason(DqStatus(verdict="RED", run_id=None))
    assert reason == "DQ audit verdict RED (DQ-run не найден)"


def test_dq_reason_fallback_when_details_unavailable():
    reason = _dq_reason(DqStatus(verdict="RED", run_id=257))
    assert reason == "DQ audit verdict RED (детали проверок недоступны)"


def test_dq_reason_fallback_when_all_checks_green_despite_verdict():
    # Рассогласование «не должно случаться» — сливается со вторым фолбэком (спека §4).
    dq = DqStatus(
        verdict="RED",
        run_id=257,
        checks=(_line("freshness:markets", "GREEN", "markets: возраст последней записи 13 мин"),),
    )
    assert _dq_reason(dq) == "DQ audit verdict RED (детали проверок недоступны)"


def test_dq_status_checks_defaults_to_empty():
    # Обратная совместимость: существующие двухаргументные вызовы не ломаются.
    assert DqStatus(verdict="AMBER", run_id=1).checks == ()


def test_prod_replay_run_257_message_names_the_cause():
    """Реплей RED-run #257 (2026-07-26). До этой правки текст был неотличим от run #217."""
    from app.analytics.proposal_render import render_proposal

    dq = DqStatus(
        verdict="RED",
        run_id=257,
        checks=(_line("freshness:markets", "RED", "markets: возраст последней записи 2713 мин"),),
    )
    inv = next(p for p in evaluate(dq, [], [], _T, rule_version=1) if p.verdict == VERDICT_INVESTIGATE_DATA)
    text = render_proposal(inv, dq.verdict, None)
    assert "2713" in text
    # Имя проверки переведено на русский (спека 2026-08-01), но причина по-прежнему НАЗВАНА —
    # именно это и стерегёт реплей: текст run #257 обязан отличаться от run #217.
    assert "рынки давно не обновлялись" in text
    assert "run #257" in text
    assert "DQ audit verdict RED" not in text  # старый безликий текст ушёл
    assert "Что делать:" in text  # сообщение заканчивается конкретным шагом
