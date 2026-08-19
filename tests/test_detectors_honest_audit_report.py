"""Unit тесты для app.jobs.detectors_honest_audit_report formatter + verdict."""
from __future__ import annotations


def _make_row(
    detector: str,
    n_dedup: int,
    per_day: float,
    per_day_1k: float,
    verdict: str,
    category: str = "crypto",
    trend: str = "",
) -> dict:
    return {
        "detector": detector,
        "category": category,
        "n_dedup": n_dedup,
        "per_day": per_day,
        "per_day_1k": per_day_1k,
        "trend": trend,
        "verdict": verdict,
    }


def test_verdict_kill():
    """$/day @ $1K < -2 → KILL."""
    from app.jobs.detectors_honest_audit_report import _verdict
    assert "KILL" in _verdict(n_dedup=1607, per_day_1k=-5.83)


def test_verdict_promote():
    """$/day @ $1K > 5 → PROMOTE candidate."""
    from app.jobs.detectors_honest_audit_report import _verdict
    assert "PROMOTE" in _verdict(n_dedup=1117, per_day_1k=7.70)


def test_verdict_marginal():
    """-2 ≤ $/day @ $1K ≤ 5 → MARGINAL."""
    from app.jobs.detectors_honest_audit_report import _verdict
    assert "MARGINAL" in _verdict(n_dedup=994, per_day_1k=3.24)


def test_verdict_insufficient_overrides_pnl():
    """n_dedup < 100 → INSUFFICIENT, даже если PnL высокий (n приоритетнее)."""
    from app.jobs.detectors_honest_audit_report import _verdict
    assert "INSUFFICIENT" in _verdict(n_dedup=54, per_day_1k=638.0)


def test_format_line_contains_fields():
    """Строка содержит детектор, n, $/day, вердикт."""
    from app.jobs.detectors_honest_audit_report import _format_line
    line = _format_line(_make_row("D36_obi_pressure", 1607, -29.16, -5.83, "❌ KILL"))
    assert "D36_obi_pressure" in line
    assert "1,607" in line or "1607" in line
    assert "KILL" in line


def test_format_message_header_and_window():
    """Сообщение содержит заголовок, дату и окно дней."""
    from app.jobs.detectors_honest_audit_report import _format_message
    rows = [_make_row("D36_obi_pressure", 1607, -29.16, -5.83, "❌ KILL")]
    msg = _format_message(rows, "2026-06-15", days=30)
    assert "Honest Audit" in msg
    assert "2026-06-15" in msg
    assert "30" in msg
    assert "D36_obi_pressure" in msg


def test_format_message_empty():
    """Пустой список (нет свежих сигналов) — без краша."""
    from app.jobs.detectors_honest_audit_report import _format_message
    msg = _format_message([], "2026-06-15", days=30)
    assert "Honest Audit" in msg
    assert "нет" in msg.lower()


def test_format_message_sorted_within_detector_by_per_day_1k():
    """Внутри одного детектора: лучшая категория (выше per_day_1k) выше худшей."""
    from app.jobs.detectors_honest_audit_report import _format_message
    rows = [
        _make_row("D14", 1000, -29.0, -5.83, "❌ KILL", category="sports"),
        _make_row("D14", 1000, 38.5, 7.70, "✅ PROMOTE candidate", category="crypto"),
    ]
    msg = _format_message(rows, "2026-06-15", days=30)
    assert msg.index("crypto") < msg.index("sports")


def test_format_message_has_weekly_checklist():
    """Отчёт с данными содержит чеклист ритма месяца (напоминание)."""
    from app.jobs.detectors_honest_audit_report import _format_message
    rows = [_make_row("D38_resolution_lag", 4659, 16.2, 3.24, "⚠️ MARGINAL")]
    msg = _format_message(rows, "2026-06-22", days=30)
    assert "ритм месяца" in msg
    assert "PROMOTE-кандидат" in msg
    assert "EXECUTION_ENABLED" in msg  # «не делай» секция
    assert "20 июня" in msg


def test_trend_label_degrading():
    """Вторая половина заметно хуже первой → 📉 деградация."""
    from app.jobs.detectors_honest_audit_report import _trend_label
    assert "📉" in _trend_label(first_half_per_day_1k=4.0, second_half_per_day_1k=-3.0)


def test_trend_label_improving():
    """Вторая половина заметно лучше первой → 📈 рост."""
    from app.jobs.detectors_honest_audit_report import _trend_label
    assert "📈" in _trend_label(first_half_per_day_1k=-3.0, second_half_per_day_1k=4.0)


def test_trend_label_stable():
    """Разница меньше порога → ➡️ стабильно."""
    from app.jobs.detectors_honest_audit_report import _trend_label
    assert "➡️" in _trend_label(first_half_per_day_1k=1.0, second_half_per_day_1k=1.4)


def test_trend_label_none_when_no_data():
    """Нет данных в одной половине (None) → пустая метка (не крашится)."""
    from app.jobs.detectors_honest_audit_report import _trend_label
    assert _trend_label(first_half_per_day_1k=None, second_half_per_day_1k=2.0) == ""


def test_trend_label_at_threshold_down():
    """delta ровно -threshold (>=) → 📉 (граница включена вниз)."""
    from app.jobs.detectors_honest_audit_report import _trend_label
    assert "📉" in _trend_label(first_half_per_day_1k=0.0, second_half_per_day_1k=-1.0)


def test_trend_label_at_threshold_up():
    """delta ровно +threshold (>=) → 📈 (граница включена вверх)."""
    from app.jobs.detectors_honest_audit_report import _trend_label
    assert "📈" in _trend_label(first_half_per_day_1k=0.0, second_half_per_day_1k=1.0)


def test_half_per_day_1k_basic():
    """sum_roi_prop=2.0 за half_days=15 → (2.0*25/15)/5 = 0.6667 $/day @$1K."""
    from app.jobs.detectors_honest_audit_report import _half_per_day_1k
    val = _half_per_day_1k(sum_roi_prop=2.0, half_days=15.0)
    assert abs(val - 0.66667) < 1e-3


def test_half_per_day_1k_none_when_zero_obs():
    """half_days=0 (нет дней) → None (нельзя делить, нет данных)."""
    from app.jobs.detectors_honest_audit_report import _half_per_day_1k
    assert _half_per_day_1k(sum_roi_prop=0.0, half_days=0.0) is None


def test_format_line_shows_category_and_trend():
    """Строка содержит категорию и trend-метку, если они заданы."""
    from app.jobs.detectors_honest_audit_report import _format_line
    line = _format_line(
        _make_row("D14_crypto_winners", 200, 10.0, 2.0, "⚠️ MARGINAL", category="crypto", trend="📉")
    )
    assert "crypto" in line
    assert "📉" in line
    assert "D14_crypto_winners" in line


def test_format_message_groups_by_detector_then_category():
    """Две категории одного детектора — обе строки присутствуют."""
    from app.jobs.detectors_honest_audit_report import _format_message
    rows = [
        _make_row("D14", 150, 5.0, 1.0, "⚠️ MARGINAL", category="crypto", trend="➡️"),
        _make_row("D14", 120, -20.0, -4.0, "❌ KILL", category="sports", trend="📉"),
    ]
    msg = _format_message(rows, "2026-06-22", days=30)
    assert "crypto" in msg
    assert "sports" in msg


def _audit_row(detector="D3_cross_mkt_arb", category="crypto", verdict="⚠️ MARGINAL", per_day=0.5, n=500):
    return {
        "detector": detector,
        "category": category,
        "n_dedup": n,
        "per_day": per_day,
        "per_day_1k": per_day / 5.0,
        "trend": "➡️",
        "verdict": verdict,
    }


def test_report_without_kills_is_fyi():
    """Плановый еженедельный отчёт без убыточных связок — FYI (§4)."""
    from app.jobs.detectors_honest_audit_report import to_notifications

    n = to_notifications([_audit_row()], days=30)[0]

    assert n.severity.value == "fyi"
    assert n.what_to_do is None
    assert n.what_if_ignored is None


def test_kill_verdict_becomes_action_required():
    """KILL — устойчивая потеря денег на достаточной выборке, это решение."""
    from app.jobs.detectors_honest_audit_report import to_notifications

    n = to_notifications(
        [_audit_row(verdict="❌ KILL", per_day=-3.0)], days=30, live_detectors={"D3_cross_mkt_arb"}
    )[0]

    assert n.severity.value == "action_required"
    assert n.what_to_do is not None


def test_kill_and_report_use_different_kinds():
    """Разные kind: иначе cooldown 25ч подавил бы «появился KILL» после планового
    отчёта или наоборот."""
    from app.jobs.detectors_honest_audit_report import to_notifications

    kill = to_notifications([_audit_row(verdict="❌ KILL", per_day=-3.0)], days=30)[0]
    plain = to_notifications([_audit_row()], days=30)[0]

    assert kill.kind != plain.kind
    assert kill.dedup_key() != plain.dedup_key()


def test_money_figure_is_monthly_and_from_actual_per_day():
    """§3: цифра фактическая (per_day при $25 stake), приведена к месяцу —
    суточная в центах не читается как решение."""
    from app.jobs.detectors_honest_audit_report import to_notifications

    n = to_notifications(
        [_audit_row(verdict="❌ KILL", per_day=-3.0)], days=30, live_detectors={"D3_cross_mkt_arb"}
    )[0]

    assert n.what_if_ignored is not None
    assert "90" in n.what_if_ignored  # 3.0 * 30
    assert "не прогноз" in n.what_if_ignored


def test_worst_cell_drives_the_money_figure():
    """Цифра берётся у худшей связки, а не суммируется по всем — сумма завысила бы."""
    from app.jobs.detectors_honest_audit_report import to_notifications

    rows = [
        _audit_row("D3_cross_mkt_arb", "crypto", "❌ KILL", per_day=-2.5),
        _audit_row("D38_resolution_lag", "sports", "❌ KILL", per_day=-10.0),
    ]
    n = to_notifications(rows, days=30, live_detectors={"D3_cross_mkt_arb", "D38_resolution_lag"})[0]

    assert "300" in n.what_if_ignored  # 10.0 * 30, худшая


def test_detector_name_is_humanized_in_kill():
    from app.jobs.detectors_honest_audit_report import to_notifications

    n = to_notifications(
        [_audit_row("D3_cross_mkt_arb", verdict="❌ KILL", per_day=-3.0)], days=30, live_detectors={"D3_cross_mkt_arb"}
    )[0]

    assert "Расхождение цен" in " ".join(n.what_happened)
    assert any("D3_cross_mkt_arb" in e for e in n.evidence)


def test_empty_rows_yield_nothing():
    from app.jobs.detectors_honest_audit_report import to_notifications

    assert to_notifications([], days=30) == []


def test_money_claim_only_for_live_detectors():
    """Прод-дефект 2026-08-04: сообщение утверждало «теряет $39/день» и «забирает
    $1180 в месяц» про M2 (stage=shadow) и D36 (stage=paused) — детекторы, которые
    деньгами не торгуют вообще.

    Цифра не выдумана (она из фактических сигналов), но подана как ТЕКУЩИЙ убыток,
    хотя это исторический счёт по сигналам. Владелец пошёл бы отключать уже
    отключённое. §3 запрещает не только выдуманные числа, но и настоящие числа,
    поданные как то, чем они не являются.
    """
    from app.jobs.detectors_honest_audit_report import to_notifications

    rows = [_audit_row("M2_liquidity_vacuum", "sports", "❌ KILL", per_day=-39.34, n=346)]
    n = to_notifications(rows, days=30, live_detectors=set())[0]

    assert n.what_if_ignored is None, "детектор не в live — денег не теряет"
    assert "не торгу" in " ".join(n.what_happened).lower()


def test_money_claim_present_for_live_detector():
    """Тот же KILL у live-детектора — деньги настоящие, цифра уместна."""
    from app.jobs.detectors_honest_audit_report import to_notifications

    rows = [_audit_row("D3_cross_mkt_arb", "crypto", "❌ KILL", per_day=-10.0, n=500)]
    n = to_notifications(rows, days=30, live_detectors={"D3_cross_mkt_arb"})[0]

    assert n.what_if_ignored is not None
    assert "300" in n.what_if_ignored


def test_kill_outside_live_is_not_action_required():
    """У непроторгованного KILL нет срочного действия: severity падает до FYI,
    иначе ACTION_REQUIRED каждую неделю про то, что и так выключено (§4: ≤2/день)."""
    from app.jobs.detectors_honest_audit_report import to_notifications

    rows = [_audit_row("M2_liquidity_vacuum", "sports", "❌ KILL", per_day=-39.34, n=346)]
    n = to_notifications(rows, days=30, live_detectors=set())[0]

    assert n.severity.value == "fyi"


def test_mixed_live_and_shadow_kills_report_only_live_money():
    """Смешанный случай: денежная цифра берётся у худшей ЖИВОЙ связки, а не
    у худшей вообще — иначе shadow-детектор задирает сумму."""
    from app.jobs.detectors_honest_audit_report import to_notifications

    rows = [
        _audit_row("M2_liquidity_vacuum", "sports", "❌ KILL", per_day=-39.34, n=346),
        _audit_row("D3_cross_mkt_arb", "crypto", "❌ KILL", per_day=-5.0, n=500),
    ]
    n = to_notifications(rows, days=30, live_detectors={"D3_cross_mkt_arb"})[0]

    assert n.severity.value == "action_required"
    assert "150" in n.what_if_ignored  # 5.0 * 30 от живого, не 39.34 * 30
