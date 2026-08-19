"""Тесты рендера advisory-сообщений (Phase 2 C3a). Чистый слой — без I/O."""

from app.analytics.policy_engine import (
    VERDICT_CLOSE_POSITION_REVIEW,
    VERDICT_INVESTIGATE_DATA,
    VERDICT_KEEP_SHADOW,
    VERDICT_OBSERVE,
    VERDICT_PAUSE_REVIEW,
    VERDICT_REDUCE_RISK_REVIEW,
    Proposal,
)
from app.analytics.proposal_render import render_proposal, should_send


def _p(verdict, scope="detector:D3", reason="x", blocked=False, blocked_by=None):
    return Proposal(
        verdict=verdict, scope=scope, reason=reason,
        evidence_run_ids=(1284,), rule_version=1, blocked=blocked, blocked_by=blocked_by,
    )


def test_render_close_position_is_orange_advisory():
    text = render_proposal(_p(VERDICT_CLOSE_POSITION_REVIEW, reason="live drawdown net ROI -33.4%"), "GREEN", None)
    assert text is not None
    assert text.startswith("🟠")
    assert "-33.4%" in text
    assert "run #1284" in text
    assert "правила v1" in text  # было "rule v1" — строка основания переведена
    assert "данные 🟢" in text


def test_render_investigate_data_is_yellow():
    text = render_proposal(_p(VERDICT_INVESTIGATE_DATA, scope="policy:dq_audit", reason="DQ audit verdict AMBER"), "AMBER", None)
    assert text is not None
    assert text.startswith("🟡")
    assert "под вопросом" in text  # AMBER передаётся словами, не константой


def test_render_keep_shadow_is_yellow():
    text = render_proposal(_p(VERDICT_KEEP_SHADOW, reason="adverse selection (block promote)"), "GREEN", None)
    assert text is not None and text.startswith("🟡")


def test_render_observe_returns_none():
    assert render_proposal(_p(VERDICT_OBSERVE, scope="portfolio:all"), "GREEN", None) is None


def test_render_blocked_returns_none():
    assert render_proposal(_p(VERDICT_CLOSE_POSITION_REVIEW, blocked=True, blocked_by="DQ_RED"), "RED", None) is None


def test_render_escapes_html_in_reason():
    text = render_proposal(_p(VERDICT_REDUCE_RISK_REVIEW, reason="a < b & c > d"), "GREEN", None)
    assert "&lt;" in text and "&gt;" in text and "&amp;" in text


def test_render_includes_ips_version_when_present():
    text = render_proposal(_p(VERDICT_REDUCE_RISK_REVIEW), "GREEN", "deadbeef00000000")
    assert "IPS deadbeef" in text


def test_should_send_dq_always():
    assert should_send(_p(VERDICT_INVESTIGATE_DATA, scope="policy:dq_audit"), enabled=False) is True
    assert should_send(_p(VERDICT_INVESTIGATE_DATA, scope="policy:dq_audit"), enabled=True) is True


def test_should_send_advisory_only_when_enabled():
    assert should_send(_p(VERDICT_CLOSE_POSITION_REVIEW), enabled=False) is False
    assert should_send(_p(VERDICT_CLOSE_POSITION_REVIEW), enabled=True) is True


def test_should_send_keep_shadow_yellow_always():
    # KEEP_SHADOW рендерится 🟡, но это не DQ — по спеке §5 «кроме 🟡 DQ молчит»,
    # значит KEEP_SHADOW в shadow НЕ шлётся (только DQ-класс — исключение).
    assert should_send(_p(VERDICT_KEEP_SHADOW), enabled=False) is False
    assert should_send(_p(VERDICT_KEEP_SHADOW), enabled=True) is True


def test_should_send_observe_never():
    assert should_send(_p(VERDICT_OBSERVE, scope="portfolio:all"), enabled=True) is False


def test_should_send_blocked_never():
    assert should_send(_p(VERDICT_CLOSE_POSITION_REVIEW, blocked=True, blocked_by="IPS_VIOLATION"), enabled=True) is False


def test_render_multiline_dq_reason_preserves_lines():
    reason = (
        "DQ RED — сработали проверки:\n"
        "• freshness:markets: RED — markets: возраст последней записи 2713 мин\n"
        "• completeness:end_date: AMBER — без end_date 8000/36319 = 22.0%"
    )
    text = render_proposal(_p(VERDICT_INVESTIGATE_DATA, scope="policy:dq_audit", reason=reason), "RED", None)
    assert text is not None
    assert text.startswith("🟡")
    # Построчность сохранена, но имена проверок переведены на русский (спека 2026-08-01).
    assert "• рынки давно не обновлялись — markets: возраст последней записи 2713 мин" in text
    assert "• у рынков не хватает даты закрытия — без end_date 8000/36319 = 22.0%" in text
    assert "данные 🔴" in text


def test_render_escapes_html_coming_from_dq_detail():
    # detail приходит из БД — экранирование обязано покрывать и его (спека §10 п.5).
    reason = "DQ RED — сработали проверки:\n• ri:orphans: RED — сирот <b>5</b> & 100"
    text = render_proposal(_p(VERDICT_INVESTIGATE_DATA, scope="policy:dq_audit", reason=reason), "RED", None)
    assert "&lt;b&gt;" in text
    assert "&amp;" in text
    assert "<b>5</b>" not in text


# ── Человеческий текст advisory (спека 2026-08-01-advisory-actionable-text-design.md) ──


def _pr(verdict: str, scope: str, reason: str, **kw) -> Proposal:
    """Хелпер новых тестов: rule_version=2 и обязательные scope/reason (в отличие от _p выше)."""
    return Proposal(verdict=verdict, scope=scope, reason=reason, rule_version=2, **kw)


def test_detector_gets_human_name_not_raw_id():
    """Машинный id заменяется человеческим именем (спека §2 п.5)."""
    text = render_proposal(
        _pr(VERDICT_PAUSE_REVIEW, "detector:D3_cross_mkt_arb", "edge decay (stage=virtual)"), "GREEN", None
    )
    assert "D3 — арбитраж между рынками" in text
    assert "detector:D3_cross_mkt_arb" not in text


def test_unknown_detector_falls_back_to_raw_id():
    """Неизвестный детектор печатается как есть — fail-open, не падаем (спека §2 п.5)."""
    text = render_proposal(
        _pr(VERDICT_PAUSE_REVIEW, "detector:D99_brand_new", "edge decay (stage=shadow)"), "GREEN", None
    )
    assert "D99_brand_new" in text


def test_category_scoped_detector_shows_category():
    text = render_proposal(
        _pr(VERDICT_REDUCE_RISK_REVIEW, "detector:D42_event_driven_ml:category:Crypto", "adverse selection in Crypto (live)"),
        "GREEN",
        None,
    )
    assert "категория: Crypto" in text


def test_every_sendable_verdict_has_action_block():
    """Каждое отправляемое сообщение обязано заканчиваться конкретным шагом (спека §1)."""
    cases = [
        (VERDICT_CLOSE_POSITION_REVIEW, "detector:D3_cross_mkt_arb", "live drawdown: net ROI -30.0% over 30d (net $-30.00 / stake $100.00, n=25)"),
        (VERDICT_REDUCE_RISK_REVIEW, "detector:D3_cross_mkt_arb", "adverse selection in Crypto (live)"),
        (VERDICT_PAUSE_REVIEW, "detector:D38_resolution_lag", "edge decay (stage=virtual)"),
        (VERDICT_KEEP_SHADOW, "detector:D36_obi_pressure", "adverse selection in Sports (block promote)"),
        (VERDICT_INVESTIGATE_DATA, "policy:dq_audit", "DQ audit verdict AMBER"),
    ]
    for verdict, scope, reason in cases:
        text = render_proposal(_p(verdict, scope, reason), "GREEN", None)
        assert "Что делать:" in text, f"{verdict}: нет блока действия"


def test_money_forecast_only_from_real_numbers():
    """Прогноз убытка считается ТОЛЬКО из фактических net/stake (спека §2 п.3, named-risk 2).

    Сообщение, которому доверяют, не может содержать выдуманную цифру.
    """
    with_numbers = render_proposal(
        _pr(VERDICT_CLOSE_POSITION_REVIEW, "detector:D3_cross_mkt_arb",
           "live drawdown: net ROI -30.8% over 30d (net $-74.47 / stake $241.90, n=39)"),
        "GREEN", None,
    )
    assert "$74" in with_numbers and "$2.48 в день" in with_numbers

    # Тот же вердикт, но reason без чисел → денежного прогноза быть НЕ должно
    without = render_proposal(
        _pr(VERDICT_CLOSE_POSITION_REVIEW, "detector:D3_cross_mkt_arb", "какая-то новая причина"), "GREEN", None
    )
    assert "Если ничего не делать" not in without


def test_dq_check_names_translated():
    """Имена проверок C0 переводятся; имя САМО содержит ':' — регулярка это учитывает."""
    text = render_proposal(
        _pr(VERDICT_INVESTIGATE_DATA, "policy:dq_audit",
           "DQ AMBER — сработали проверки:\n• freshness:detector_signals: AMBER — возраст 400 мин"),
        "AMBER", None,
    )
    assert "сигналы детекторов давно не обновлялись" in text
    assert "freshness:detector_signals" not in text


def test_unparseable_reason_is_preserved():
    """Fail-open: неизвестный формат reason печатается как есть, не теряется (спека §2 п.6)."""
    text = render_proposal(
        _pr(VERDICT_PAUSE_REVIEW, "detector:D38_resolution_lag", "совершенно новый формат причины"), "GREEN", None
    )
    assert "совершенно новый формат причины" in text


def test_html_is_escaped_in_untrusted_parts():
    """Категории/детали попадают в текст — экранирование обязательно (named-risk 3)."""
    text = render_proposal(
        _pr(VERDICT_KEEP_SHADOW, "detector:D36_obi_pressure", "adverse selection in <script>x</script> (block promote)"),
        "GREEN", None,
    )
    assert "<script>" not in text
    assert "&lt;script&gt;" in text


def test_message_fits_telegram_limit():
    """Telegram режет на 4096 символов (named-risk 5)."""
    text = render_proposal(
        _pr(VERDICT_CLOSE_POSITION_REVIEW, "detector:STANDALONE_V2_LGB:category:" + "Очень длинная категория " * 8,
           "live drawdown: net ROI -30.8% over 30d (net $-74.47 / stake $241.90, n=39)"),
        "GREEN", None,
    )
    assert len(text) < 4096
