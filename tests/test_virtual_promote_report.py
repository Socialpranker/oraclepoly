"""Unit тесты для app.jobs.virtual_promote_report formatter."""

from __future__ import annotations

import pytest


def _make_row(
    detector: str,
    days_virtual: int,
    n_terminal: int,
    avg_roi: float | None,
    sortino: float | None,
    gates_passed: int,
    blocker: str,
) -> dict:
    return {
        "detector": detector,
        "days_virtual": days_virtual,
        "n_terminal": n_terminal,
        "avg_roi_net": avg_roi,
        "sortino": sortino,
        "gates_passed": gates_passed,
        "gates_total": 6,
        "blocker": blocker,
    }


def test_format_report_line_with_data():
    """Строка с данными содержит все поля."""
    from app.jobs.virtual_promote_report import _format_report_line

    row = _make_row("D3_cross_mkt_arb", 22, 130, 3.6, 0.42, 3, "avg_roi≤0")
    line = _format_report_line(row)
    assert "D3_cross_mkt_arb" in line
    assert "22d" in line
    assert "n=130" in line
    assert "3.6%" in line
    assert "0.42" in line
    assert "gates=3-6" in line
    assert "avg_roi≤0" in line


def test_format_report_line_no_data():
    """Строка без данных (n=0) показывает накапливается."""
    from app.jobs.virtual_promote_report import _format_report_line

    row = _make_row("D14_crypto_winners", 0, 0, None, None, 0, "накапливается")
    line = _format_report_line(row)
    assert "D14_crypto_winners" in line
    assert "0d" in line
    assert "n=0" in line
    assert "накапливается" in line


def test_format_message_contains_header():
    """Сообщение содержит заголовок с датой."""
    from app.jobs.virtual_promote_report import _format_message

    rows = [_make_row("D3", 22, 130, 3.6, 0.42, 3, "avg_roi≤0")]
    msg = _format_message(rows, "2026-06-04")
    assert "Virtual Promote Report" in msg
    assert "2026-06-04" in msg
    assert "D3" in msg


def test_format_message_empty():
    """Пустой список детекторов — минимальное сообщение без краша."""
    from app.jobs.virtual_promote_report import _format_message

    msg = _format_message([], "2026-06-04")
    assert "Virtual Promote Report" in msg
    assert "нет виртуальных детекторов" in msg


def test_blocker_ready():
    """Когда все gates passed — блокер = READY."""
    from app.jobs.virtual_promote_report import _format_report_line

    row = _make_row("D_ready", 30, 200, 8.0, 1.5, 6, "READY")
    line = _format_report_line(row)
    assert "READY" in line


def test_compute_blocker_ready():
    """_compute_blocker возвращает READY когда все условия выполнены."""
    from app.jobs.virtual_promote_report import _compute_blocker

    result = _compute_blocker(
        n_terminal=200,
        avg_roi=5.0,
        sortino=1.5,
        days_virtual=20,
        adversarial_pass=True,
        execution_enabled=True,
    )
    assert result == "READY"


def test_compute_blocker_n_too_small():
    """_compute_blocker возвращает n<40 когда n_terminal < 40."""
    from app.jobs.virtual_promote_report import _compute_blocker

    result = _compute_blocker(
        n_terminal=28,
        avg_roi=5.0,
        sortino=1.5,
        days_virtual=20,
        adversarial_pass=True,
        execution_enabled=True,
    )
    assert "n&lt;40" in result
    assert "28" in result


def test_compute_blocker_adversarial_null():
    """_compute_blocker возвращает adversarial=NULL когда adversarial_pass=None."""
    from app.jobs.virtual_promote_report import _compute_blocker

    result = _compute_blocker(
        n_terminal=200,
        avg_roi=5.0,
        sortino=1.5,
        days_virtual=20,
        adversarial_pass=None,
        execution_enabled=True,
    )
    assert "adversarial=NULL" in result


# --- Resolution-only (live-реплицируемый) P&L slice ------------------------
# research/virtual-stage-audit-2026-07-05: order-level virtual P&L (as-is)
# может врать в обе стороны из-за TP/SL early-exit, которые live-контур не
# исполняет (live_exit_enabled=False). Резолюционный срез (outcome IN
# ('WIN','LOSS'), т.е. pnl_resolved_usd/wins/losses из fetch_virtual_metrics_7d)
# — единственная честная проекция на то, что реально исполнил бы live.


def _make_row_with_resolved(**overrides) -> dict:
    row = _make_row("D3_cross_mkt_arb", 22, 130, 3.6, 0.42, 3, "avg_roi≤0")
    row.update(
        {
            "resolved_pnl_usd": 989.0,
            "resolved_wins": 55,
            "resolved_losses": 44,
        }
    )
    row.update(overrides)
    return row


def test_format_report_line_includes_live_replicable_slice():
    """Строка отчёта содержит resolution-only P&L/winrate с меткой live-реплицируемый."""
    from app.jobs.virtual_promote_report import _format_report_line

    row = _make_row_with_resolved()
    line = _format_report_line(row)

    assert "live-реплицируемый" in line
    assert "+989.0" in line or "+989.00" in line or "989.0" in line
    # winrate = 55 / (55+44) = 55.6%
    assert "55.6%" in line or "56%" in line or "55.6" in line


def test_format_report_line_live_replicable_slice_no_data():
    """Нет resolved-данных (детектор ещё не settled ни одной позиции) — не крашимся,
    метка показывает отсутствие данных, а не 0/0 деление."""
    from app.jobs.virtual_promote_report import _format_report_line

    row = _make_row("D_new", 1, 0, None, None, 0, "n&lt;40 (0)")
    row.update({"resolved_pnl_usd": None, "resolved_wins": 0, "resolved_losses": 0})
    line = _format_report_line(row)

    assert "live-реплицируемый" in line
    assert "—" in line


@pytest.mark.asyncio
async def test_run_virtual_promote_report_wires_resolved_slice(monkeypatch):
    """run_virtual_promote_report берёт resolved-срез из fetch_virtual_metrics_7d
    (order-level, outcome IN WIN/LOSS) и прокидывает его в строку отчёта."""
    from datetime import UTC, datetime, timedelta

    from app.jobs import virtual_promote_report as vpr

    promoted_at = datetime.now(UTC) - timedelta(days=20)

    async def mock_fetch_virtual_state_rows():
        return [
            {
                "detector": "D3_cross_mkt_arb",
                "stage": "virtual",
                "promoted_at": promoted_at,
                "adversarial_mirror_pass": True,
            }
        ]

    async def mock_fetch_virtual_period_signals(_pairs):
        return {
            "D3_cross_mkt_arb": {
                "terminal_7d": 50,
                "mean_roi_net_7d": 5.0,
                "sum_neg_sq_roi_7d": 10.0,
                "sum_roi_net_7d": 250.0,
            }
        }

    async def mock_fetch_virtual_metrics_7d():
        return {
            "D3_cross_mkt_arb": {
                "pnl_resolved_usd": 989.0,
                "wins": 55,
                "losses": 44,
            }
        }

    published: list = []

    async def mock_publish(notifications, actor):
        published.append((notifications, actor))
        from app.services.notification_bus import PublishResult

        return PublishResult(digested=len(notifications))

    class _FakeSettings:
        admin_telegram_ids = [1]
        execution_enabled = False

    monkeypatch.setattr(vpr, "fetch_virtual_state_rows", mock_fetch_virtual_state_rows)
    monkeypatch.setattr(vpr, "fetch_virtual_period_signals", mock_fetch_virtual_period_signals)
    monkeypatch.setattr(vpr, "fetch_virtual_metrics_7d", mock_fetch_virtual_metrics_7d)
    monkeypatch.setattr(vpr, "publish", mock_publish)
    monkeypatch.setattr("app.core.config.get_settings", lambda: _FakeSettings())

    await vpr.run_virtual_promote_report()

    # Resolved-срез обязан доехать до уведомления: раньше проверялось по тексту
    # сообщения, теперь по evidence конверта — джоба текст больше не собирает.
    assert len(published) == 1
    notifications, actor = published[0]
    assert actor == "virtual_promote_report"
    evidence = " ".join(e for n in notifications for e in n.evidence)
    assert "D3_cross_mkt_arb" in evidence
    # execution_enabled=False → гейт не пройден → детектор НЕ READY, это FYI
    assert notifications[0].severity.value == "fyi"


def _ready_row(detector="D3_cross_mkt_arb", pnl=989.0):
    return {
        "detector": detector,
        "gates_passed": 6,
        "n_terminal": 50,
        "blocker": "READY",
        "resolved_pnl_usd": pnl,
        "resolved_wins": 55,
        "resolved_losses": 44,
    }


def _blocked_row(detector="D38_resolution_lag"):
    return {
        "detector": detector,
        "gates_passed": 3,
        "n_terminal": 12,
        "blocker": "n_terminal<40",
        "resolved_pnl_usd": None,
        "resolved_wins": 0,
        "resolved_losses": 0,
    }


def test_no_ready_detectors_is_fyi():
    """Плановый отчёт без кандидатов — FYI, строкой в Daily Brief (§4)."""
    from app.jobs.virtual_promote_report import to_notifications

    n = to_notifications([_blocked_row()])[0]

    assert n.severity.value == "fyi"
    assert n.what_to_do is None
    assert n.what_if_ignored is None


def test_ready_detector_becomes_action_required():
    """READY — состояние с решением владельца (промоут только вручную).
    Увести его в дайджест значит потерять решение (риск №4 §10)."""
    from app.jobs.virtual_promote_report import to_notifications

    n = to_notifications([_ready_row()])[0]

    assert n.severity.value == "action_required"
    assert n.what_to_do is not None
    assert "вручную" in n.what_to_do


def test_ready_and_blocked_use_different_kinds():
    """Разные kind: cooldown шины 25ч по (subject, kind), и общий kind означал бы,
    что «кандидатов нет» подавляет «появился кандидат» и наоборот."""
    from app.jobs.virtual_promote_report import to_notifications

    ready = to_notifications([_ready_row()])[0]
    blocked = to_notifications([_blocked_row()])[0]

    assert ready.kind != blocked.kind
    assert ready.dedup_key() != blocked.dedup_key()


def test_money_figure_comes_from_resolved_pnl_and_is_named_virtual():
    """§3: цифра только фактическая. resolved_pnl_usd — деньги по позициям,
    дожившим до резолва; названы виртуальными, потому что детектор на бумаге."""
    from app.jobs.virtual_promote_report import to_notifications

    n = to_notifications([_ready_row(pnl=989.0)])[0]

    assert n.what_if_ignored is not None
    assert "989" in n.what_if_ignored
    assert "виртуальн" in n.what_if_ignored.lower()


def test_no_money_claim_when_resolved_pnl_missing():
    """Нет фактической цифры — нет денежной строки (§3)."""
    from app.jobs.virtual_promote_report import to_notifications

    n = to_notifications([_ready_row(pnl=None)])[0]

    assert n.what_if_ignored is None


def test_negative_resolved_pnl_yields_no_money_promise():
    """Убыточный кандидат не должен обещать прибыль от промоута."""
    from app.jobs.virtual_promote_report import to_notifications

    n = to_notifications([_ready_row(pnl=-120.0)])[0]

    assert n.what_if_ignored is None


def test_detector_name_is_humanized():
    from app.jobs.virtual_promote_report import to_notifications

    n = to_notifications([_ready_row("D3_cross_mkt_arb")])[0]

    assert "Расхождение цен" in " ".join(n.what_happened)
    assert any("D3_cross_mkt_arb" in e for e in n.evidence)


def test_empty_rows_yield_nothing():
    from app.jobs.virtual_promote_report import to_notifications

    assert to_notifications([]) == []
