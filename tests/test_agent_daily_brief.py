"""Tests для app.jobs.agent_daily_brief."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.jobs.agent_daily_brief import _LATEST_AUDIT_RUNS_SQL, _build_brief, _is_red_verdict, run_agent_daily_brief

_NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def _run(run_id, kind="audit", name="d42_portfolio_roi_replay", verdict="ВНИМАНИЕ: тестовый red-verdict"):
    return {"id": run_id, "kind": kind, "name": name, "verdict": verdict, "started_at": _NOW, "finished_at": _NOW}


def _claim(claim_id, statement="stale claim", expires_at=None):
    return {
        "id": claim_id,
        "statement": statement,
        "scope": "detector:d42",
        "expires_at": expires_at or (_NOW + timedelta(days=1)),
        "last_verified_at": _NOW - timedelta(days=89),
    }


def test_build_brief_empty_tables():
    """Пустые agent_runs/claims_expiry_queue — бриф генерируется без ошибок (Acceptance §1)."""
    msg = _build_brief([], [], [], _NOW, dq_status=None, shadow_proposals=[])
    assert "Daily Brief" in msg
    assert "Нет прогонов за последние 24ч." in msg
    assert "Ни один кейс ещё не прогнан." in msg
    assert "Нет клеймов, протухающих в ближайшие 3 дня." in msg
    # Без кнопок/интерактива (Acceptance: Daily-бриф НЕ содержит кнопок)
    assert "callback_data" not in msg
    assert "<button" not in msg.lower()


def test_build_brief_recent_runs_carry_source_id():
    """Каждая строка прогона несёт agent_runs.id — §8 acceptance source/query hash."""
    # Verdict — реальный текст из audit_case2_d38_anomaly_stage_replay.py:187.
    runs = [
        _run(
            101,
            name="d38_anomaly_kill_stage_unaware",
            verdict="ЛОЖНЫЙ KILL воспроизведён.",
        )
    ]
    msg = _build_brief(runs, [], [], _NOW, dq_status=None, shadow_proposals=[])
    assert "agent_runs.id=101" in msg
    assert "d38_anomaly_kill_stage_unaware" in msg


def test_build_brief_expiring_claims_carry_source_id():
    """Протухающие клеймы несут agent_claims.id — §8 acceptance."""
    claims = [_claim(55, statement="D42 sum_roi_pct — известное расхождение метода")]
    msg = _build_brief([], claims, [], _NOW, dq_status=None, shadow_proposals=[])
    assert "agent_claims.id=55" in msg
    assert "D42 sum_roi_pct — известное расхождение метода" in msg
    assert "expires 2026-07-21" in msg


def test_build_brief_replay_cases_green_red_split():
    """6 replay-кейсов: зелёные/красные по verdict-эвристике, каждый со своим id.

    Verdict-фрагменты — реальные тексты из скриптов (не синтетика):
      - id=1: audit_case2_d38_anomaly_stage_replay.py:187 ("ЛОЖНЫЙ KILL воспроизведён").
      - id=2: audit_case3_hold_vs_tpsl_replay.py, positive-ветка (alternatives_worse_or_equal),
        строки 254-256 ("Trailing/... все хуже (или не лучше) hold-to-resolution").
    """
    latest = [
        _run(1, name="d38_anomaly_kill_stage_unaware", verdict="ЛОЖНЫЙ KILL воспроизведён."),
        _run(
            2,
            name="hold_vs_tpsl_replay",
            verdict=(
                "Trailing/time-decay/partial-exit все хуже (или не лучше) hold-to-resolution "
                "на этом наборе позиций — согласуется с memory "
                "exit-strategies-hold-to-resolution-confirmed-2026-07-03."
            ),
        ),
    ]
    msg = _build_brief([], [], latest, _NOW, dq_status=None, shadow_proposals=[])
    assert "🔴" in msg
    assert "🟢" in msg
    assert "agent_runs.id=1" in msg
    assert "agent_runs.id=2" in msg
    assert "1 зелёных, 1 красных из 2 прогнанных" in msg
    assert "4 из 6 ещё не прогнаны" in msg


def test_build_brief_all_six_cases_no_missing_note():
    """Когда все 6 кейсов прогнаны — нет строки про "ещё не прогнаны"."""
    latest = [_run(i, name=f"case_{i}", verdict="OK") for i in range(1, 7)]
    msg = _build_brief([], [], latest, _NOW, dq_status=None, shadow_proposals=[])
    assert "ещё не прогнаны" not in msg
    assert "6 зелёных, 0 красных из 6 прогнанных" in msg


def test_latest_audit_runs_sql_excludes_dq_audit_daily():
    """dq_audit_daily (Phase 2 C0 operational DQ) не должен попадать в раздел
    "Replay-кейсы Phase 0" — у него свой TG-канал алертов, и _is_red_verdict
    (free-text эвристика на 6 replay-скриптов) вернёт False для голого 'RED',
    из-за чего живой DQ RED отрендерился бы 🟢. Отдельная секция DQ-статуса —
    скоуп C1, не C0 (whole-branch review fix)."""
    assert "name <> 'dq_audit_daily'" in _LATEST_AUDIT_RUNS_SQL


def test_is_red_verdict_markers():
    """Синтетические маркеры на будущее (Phase 0 не ограничивается 6 текущими кейсами) —
    "refuted"/"annulled" не встречаются буквально ни в одном из 6 текущих скриптов
    (см. test_is_red_verdict_matches_real_case* ниже для действующих кейсов), но
    остаются разумными red-сигналами для следующих replay-аудитов."""
    assert _is_red_verdict("Hypothesis REFUTED — 0/228 WIN") is True
    assert _is_red_verdict("D44 KEEPER annulled постмортемом") is True
    assert _is_red_verdict("Hold-to-resolution подтверждена, +127%") is False
    assert _is_red_verdict(None) is False
    assert _is_red_verdict("") is False


def test_is_red_verdict_matches_real_case2_d38_anomaly():
    """Реальные фрагменты verdict_parts из audit_case2_d38_anomaly_stage_replay.py:184-191."""
    # Строка 187: buggy_kill_triggered=True — реальное подтверждение бага.
    buggy_kill_true = (
        "buggy SQL (без JOIN detector_state): D38_resolution_lag rejected_recent max=12/min "
        "(>threshold=5) → ЛОЖНЫЙ KILL воспроизведён."
    )
    assert _is_red_verdict(buggy_kill_true) is True

    # Строка 187, ветка else — kill не воспроизведён (unexpected, но НЕ red-паттерн бага).
    buggy_kill_false = (
        "buggy SQL (без JOIN detector_state): D38_resolution_lag rejected_recent max=2/min "
        "(<=threshold=5) → kill НЕ воспроизведён (unexpected)."
    )
    assert _is_red_verdict(buggy_kill_false) is False

    # Строка 191: D3 ложноположительный кейс — "ВНИМАНИЕ: D3 превышает порог."
    d3_attention = "D3_cross_mkt_arb (live): buggy max=1/min, fixed max=9/min — ВНИМАНИЕ: D3 превышает порог."
    assert _is_red_verdict(d3_attention) is True


def test_is_red_verdict_matches_real_case3_hold_vs_tpsl():
    """Реальные фрагменты verdict_lines из audit_case3_hold_vs_tpsl_replay.py:253-263."""
    # Негативная ветка (alternatives_worse_or_equal=False, строка 258).
    negative_branch = (
        "ВНИМАНИЕ: минимум одна альтернатива (trailing/decay/partial) превзошла "
        "hold-to-resolution на этом прогоне — расхождение с memory "
        "exit-strategies-hold-to-resolution-confirmed-2026-07-03, требует ручного разбора "
        "перед тем как считать клейм подтверждённым."
    )
    assert _is_red_verdict(negative_branch) is True

    # Позитивная ветка (alternatives_worse_or_equal=True, строки 254-256) — содержит
    # слово "хуже" в прозе про ПОЗИТИВНЫЙ исход (альтернативы хуже hold = hold победил),
    # маркер "(хуже," (со скобкой+запятой) её не ловит намеренно.
    positive_branch = (
        "Trailing/time-decay/partial-exit все хуже (или не лучше) hold-to-resolution "
        "на этом наборе позиций — согласуется с memory "
        "exit-strategies-hold-to-resolution-confirmed-2026-07-03."
    )
    assert _is_red_verdict(positive_branch) is False


def test_is_red_verdict_matches_real_case1_d42_portfolio_roi():
    """Реальные фрагменты _build_verdict() из audit_case1_d42_portfolio_roi_replay.py:250-303."""
    # Строка 250: "(баг-репродукция)" — это label метрики (sum vs mean ROI), всегда
    # присутствует независимо от реального исхода, НЕ надёжный red/green сигнал сам
    # по себе — намеренно НЕ используется как маркер.
    always_present_label = (
        "sum_roi_pct (баг-репродукция) = -1058.1% на n_terminal=157 — это СУММА "
        "per-signal realized_roi_net, не портфельный ROI."
    )
    assert _is_red_verdict(always_present_label) is False

    # Строка 278-280: месячная деградация, degraded=True — реальный red-сигнал.
    degradation_worse = "Месячная деградация (2026-06 → 2026-07): mean_roi_pct 1.20% → -3.40% (ХУЖЕ, n_terminal 42→38)."
    assert _is_red_verdict(degradation_worse) is True

    # Та же строка, degraded=False — "не хуже" содержит "хуже" как substring,
    # маркер "(хуже," с запятой явно не матчит "(не хуже,".
    degradation_not_worse = (
        "Месячная деградация (2026-06 → 2026-07): mean_roi_pct -3.40% → 1.20% (не хуже, n_terminal 38→42)."
    )
    assert _is_red_verdict(degradation_not_worse) is False

    # Строки 297-299: stop_loss-exits целиком делают убыток — реальный red-сигнал.
    stop_loss_entirely_loss = "stop_loss-exits дали net_pnl=-120.50 — ЦЕЛИКОМ делают убыток"
    assert _is_red_verdict(stop_loss_entirely_loss) is True


def test_is_red_verdict_matches_real_case6_uma_dispute_overlap():
    """Реальные фрагменты verdict_lines из audit_case6_uma_dispute_overlap.py:150-178."""
    # Ветка n_overlap > 0 (строка 154) — риск материализовался на реальных позициях.
    overlap_found = (
        "НАЙДЕНО: 3 live-ордер(ов) (execution_mode IN ('maker', 'taker'), "
        "status IN (...)) исторически пересекались с рынком, у которого "
        "dispute_count >= 1 в UMA-резолюции."
    )
    assert _is_red_verdict(overlap_found) is True

    # Ветка n_overlap == 0 (строка 167) — "НЕ НАЙДЕНО:" содержит "найдено:" как
    # substring, маркер явно исключает отрицание перед ним.
    overlap_not_found = (
        "НЕ НАЙДЕНО: 0 live-ордеров (execution_mode IN ('maker', 'taker'), "
        "status IN (...)) исторически пересекались с рынком, у которого "
        "dispute_count >= 1."
    )
    assert _is_red_verdict(overlap_not_found) is False


def test_is_red_verdict_case4_g18_and_case5_debt_sweep_are_informational():
    """audit_case4 (g18) и audit_case5 (may_debt_sweep) — verdict-тексты
    ИНФОРМАЦИОННЫЕ/counterfactual-измерительные, без red/green исхода в
    тексте. Реальные фрагменты из audit_case4_g18_money_contribution.py:561-564
    и audit_case5_may_debt_sweep.py:503-505 — оба должны оставаться green
    (не притягиваем маркер специально под них, см. докстринг _is_red_verdict)."""
    case4_verdict_tag_line = (
        "verdict_tag: counterfactual_unmeasured_in_reality\n"
        "G18 contribution to realized (live) PnL = $0 — D14=shadow "
        "(down-staged 2026-06-13), D36=paused (2026-06-24, ΣROI<0+Sortino<0), "
        "execution_mode='live' содержит НОЛЬ строк для обоих детекторов за всю "
        "историю (verified 2026-07-20)."
    )
    assert _is_red_verdict(case4_verdict_tag_line) is False

    case5_inventory_line = (
        "ИНВЕНТАРИЗАЦИЯ, НЕ ПРИГОВОР: sweep помечает кандидатов на ревалидацию "
        "(cutoff=2026-05-01), не выносит окончательный вердикт 'неверно' без "
        "реального пересчёта каждого элемента — это работа других кейсов/будущих аудитов."
    )
    assert _is_red_verdict(case5_inventory_line) is False


def test_build_brief_expiring_claims_truncated_and_counted():
    """>10 протухающих клеймов — показываем первые 10 + "и ещё N"."""
    claims = [_claim(i, statement=f"claim {i}") for i in range(15)]
    msg = _build_brief([], claims, [], _NOW, dq_status=None, shadow_proposals=[])
    assert "и ещё 5" in msg


def test_build_brief_html_escapes_lt_for_telegram():
    """TG parse_mode=HTML — '<3 дня' должен быть &lt;3 дня, иначе TG отбрасывает."""
    msg = _build_brief([], [], [], _NOW, dq_status=None, shadow_proposals=[])
    assert "&lt;3 дня" in msg


def test_brief_renders_real_dq_status_when_present():
    text = _build_brief(
        [],
        [],
        [],
        datetime(2026, 7, 24, 7, 45, tzinfo=UTC),
        dq_status={"id": 166, "verdict": "GREEN", "started_at": datetime(2026, 7, 24, 6, 45, tzinfo=UTC)},
        shadow_proposals=[],
    )
    assert "DQ-статус" in text
    assert "GREEN" in text
    assert "agent_runs.id=166" in text
    assert "Суррогат" not in text  # заглушка заменена


def test_brief_falls_back_to_stub_without_dq_run():
    text = _build_brief([], [], [], datetime(2026, 7, 24, 7, 45, tzinfo=UTC), dq_status=None, shadow_proposals=[])
    assert "DQ-run не найден" in text


def test_brief_renders_shadow_proposals_digest():
    proposals = [
        {"id": 201, "name": "policy_engine:detector:D38_resolution_lag", "verdict": "PAUSE_REVIEW"},
        {"id": 202, "name": "policy_engine:detector:D3_cross_mkt_arb", "verdict": "CLOSE_POSITION_REVIEW"},
    ]
    text = _build_brief(
        [], [], [], datetime(2026, 7, 24, 7, 45, tzinfo=UTC), dq_status=None, shadow_proposals=proposals
    )
    assert "Policy engine (shadow)" in text
    assert "2 proposals" in text
    assert "PAUSE_REVIEW" in text and "CLOSE_POSITION_REVIEW" in text


def test_brief_shadow_section_quiet_when_empty():
    text = _build_brief([], [], [], datetime(2026, 7, 24, 7, 45, tzinfo=UTC), dq_status=None, shadow_proposals=[])
    assert "0 proposals за 24ч" in text


def test_brief_investigate_data_proposal_not_marked_silent():
    """Whole-branch ревью C3a fix: 🟡 DQ (INVESTIGATE_DATA) уходит в TG всегда (спека §5) —
    бриф не должен писать «в тени»/«молчит» для него, даже если metrics.shadow=true."""
    proposals = [
        {
            "id": 301,
            "name": "policy_engine:policy:dq_audit",
            "verdict": "INVESTIGATE_DATA",
            "shadow": True,
        }
    ]
    text = _build_brief(
        [], [], [], datetime(2026, 7, 24, 7, 45, tzinfo=UTC), dq_status=None, shadow_proposals=proposals
    )
    lines = [line for line in text.splitlines() if "INVESTIGATE_DATA" in line]
    assert lines, "ожидалась строка с INVESTIGATE_DATA proposal"
    assert "в тени" not in lines[0]
    assert "ушло" in lines[0]
    assert "1 отправлено" in text
    assert "0 в тени" in text


def test_brief_orange_shadow_proposal_marked_silent():
    """🟠 REDUCE_RISK_REVIEW с metrics.shadow=true реально молчит (enabled=False) —
    бриф должен честно пометить его «в тени», не «ушло»."""
    proposals = [
        {
            "id": 302,
            "name": "policy_engine:detector:D3_cross_mkt_arb",
            "verdict": "REDUCE_RISK_REVIEW",
            "shadow": True,
        }
    ]
    text = _build_brief(
        [], [], [], datetime(2026, 7, 24, 7, 45, tzinfo=UTC), dq_status=None, shadow_proposals=proposals
    )
    lines = [line for line in text.splitlines() if "REDUCE_RISK_REVIEW" in line]
    assert lines, "ожидалась строка с REDUCE_RISK_REVIEW proposal"
    assert "в тени" in lines[0]
    assert "ушло" not in lines[0]
    assert "0 отправлено" in text
    assert "1 в тени" in text


def test_brief_nags_when_ips_absent():
    text = _build_brief([], [], [], datetime.now(UTC), None, [], ips_status="absent")
    assert "IPS не заполнен" in text
    assert "owner_ips.template.md" in text


def test_brief_nags_on_ips_parse_error():
    text = _build_brief([], [], [], datetime.now(UTC), None, [], ips_status="parse_error")
    assert "IPS не парсится" in text


def test_brief_silent_when_ips_active():
    text = _build_brief([], [], [], datetime.now(UTC), None, [], ips_status="active")
    assert "IPS" not in text


def test_ips_file_status_degrades_on_read_error():
    """Сбой чтения файла (PermissionError и т.п.) не роняет бриф — деградация в parse_error."""
    from app.jobs.agent_daily_brief import _ips_file_status

    with patch("app.jobs.agent_daily_brief.load_ips_file", side_effect=PermissionError("denied")):
        assert _ips_file_status() == "parse_error"


@pytest.mark.asyncio
async def test_run_agent_daily_brief_smoke_empty_db():
    """Smoke: run_agent_daily_brief отрабатывает на пустых таблицах, шлёт TG, REFRESH вызван."""
    with (
        patch("app.jobs.agent_daily_brief.execute", new=AsyncMock(return_value=None)) as m_execute,
        patch("app.jobs.agent_daily_brief.fetch", new=AsyncMock(return_value=[])),
        patch("app.jobs.agent_daily_brief.fetchrow", new=AsyncMock(return_value=None)),
        patch("app.tasks.notifications.send_admin_message", new=AsyncMock()) as m_send,
    ):
        result = await run_agent_daily_brief()

    # REFRESH вызван; пометка FYI consumed при пустой очереди НЕ вызывается.
    refresh_calls = [c for c in m_execute.await_args_list if "REFRESH" in str(c[0][0])]
    assert len(refresh_calls) == 1
    m_send.assert_awaited_once()
    assert result == {"n_recent_runs": 0, "n_expiring_claims": 0, "n_audit_cases": 0, "n_fyi": 0}


def test_brief_renders_fyi_lines():
    """Риск №1 §10: FYI обязан ПОЯВИТЬСЯ в тексте брифа, иначе источник, переведённый
    в FYI, молча перестаёт приходить."""
    fyi = [
        {"id": 1, "digest_line": "ℹ️ <b>База данных</b>: Таблица растёт — markets 900 МБ"},
        {"id": 2, "digest_line": "ℹ️ <b>D42</b>: Виртуальный прогон за сутки"},
    ]
    text = _build_brief([], [], [], _NOW, None, [], fyi_lines=fyi)

    assert "markets 900 МБ" in text
    assert "Виртуальный прогон за сутки" in text


def test_brief_fyi_section_quiet_when_empty():
    """Пустая очередь не должна давать пустую секцию-заглушку в брифе."""
    text = _build_brief([], [], [], _NOW, None, [], fyi_lines=[])

    assert "Сводка за сутки" not in text


@pytest.mark.asyncio
async def test_brief_marks_fyi_consumed_only_for_rendered_rows():
    """Помечаем consumed ровно те id, что попали в текст: пометить лишнее — потерять
    сообщение навсегда (оно уже не попадёт в следующий бриф)."""
    fyi = [{"id": 7, "digest_line": "ℹ️ <b>X</b>: строка"}]

    async def _fetch(sql, *args):
        return fyi if "notification_fyi_queue" in sql else []

    with (
        patch("app.jobs.agent_daily_brief.execute", new=AsyncMock(return_value=None)) as m_execute,
        patch("app.jobs.agent_daily_brief.fetch", new=AsyncMock(side_effect=_fetch)),
        patch("app.jobs.agent_daily_brief.fetchrow", new=AsyncMock(return_value=None)),
        patch("app.tasks.notifications.send_admin_message", new=AsyncMock()),
    ):
        result = await run_agent_daily_brief()

    consume_calls = [c for c in m_execute.await_args_list if "consumed_at" in str(c[0][0])]
    assert len(consume_calls) == 1
    assert consume_calls[0][0][1] == [7]
    assert result["n_fyi"] == 1


@pytest.mark.asyncio
async def test_brief_does_not_consume_fyi_when_send_failed():
    """Сбой отправки брифа не должен «съедать» очередь: иначе FYI за сутки исчезают,
    а сообщение до владельца не дошло."""
    fyi = [{"id": 7, "digest_line": "ℹ️ <b>X</b>: строка"}]

    async def _fetch(sql, *args):
        return fyi if "notification_fyi_queue" in sql else []

    with (
        patch("app.jobs.agent_daily_brief.execute", new=AsyncMock(return_value=None)) as m_execute,
        patch("app.jobs.agent_daily_brief.fetch", new=AsyncMock(side_effect=_fetch)),
        patch("app.jobs.agent_daily_brief.fetchrow", new=AsyncMock(return_value=None)),
        patch("app.tasks.notifications.send_admin_message", new=AsyncMock(side_effect=RuntimeError("tg down"))),
        pytest.raises(RuntimeError),
    ):
        await run_agent_daily_brief()

    consume_calls = [c for c in m_execute.await_args_list if "consumed_at" in str(c[0][0])]
    assert consume_calls == []


@pytest.mark.asyncio
async def test_run_agent_daily_brief_survives_refresh_failure():
    """REFRESH claims_expiry_queue падает (MV миграция ещё не применена) — бриф не падает целиком."""
    with (
        patch("app.jobs.agent_daily_brief.execute", new=AsyncMock(side_effect=Exception("relation does not exist"))),
        patch("app.jobs.agent_daily_brief.fetch", new=AsyncMock(return_value=[])),
        patch("app.jobs.agent_daily_brief.fetchrow", new=AsyncMock(return_value=None)),
        patch("app.tasks.notifications.send_admin_message", new=AsyncMock()) as m_send,
    ):
        result = await run_agent_daily_brief()

    m_send.assert_awaited_once()
    assert result["n_recent_runs"] == 0
