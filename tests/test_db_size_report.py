"""Unit тесты для app.jobs.db_size_report — конверт уведомлений (спека §3)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.analytics.notification import Severity
from app.jobs.db_size_report import to_notifications


def _tables():
    return [
        {"table_name": "detector_signal_price_snapshots", "bytes": 3_076_000_000, "rows": 14_878_674},
        {"table_name": "markets", "bytes": 990_000_000, "rows": 234_721},
    ]


def test_report_below_threshold_is_fyi():
    """Плановый отчёт без превышения — FYI: идёт строкой в Daily Brief, а не
    отдельным сообщением (спека §4)."""
    n = to_notifications(10_000_000_000, _tables())[0]

    assert n.severity == Severity.FYI
    assert n.what_to_do is None
    assert "10.00 ГБ" in " ".join(n.what_happened)


def test_report_over_threshold_becomes_action_required():
    """Превышение порога — состояние с действием. Уводить его в дайджест значит
    потерять (риск №4 §10)."""
    n = to_notifications(45_000_000_000, _tables())[0]

    assert n.severity == Severity.ACTION_REQUIRED
    assert n.what_to_do is not None
    assert "retention" in n.what_to_do


def test_report_never_invents_money_figure():
    """Спека §3: what_if_ignored обязан быть None, если фактической цифры нет."""
    assert to_notifications(10_000_000_000, _tables())[0].what_if_ignored is None
    assert to_notifications(45_000_000_000, _tables())[0].what_if_ignored is None


def test_report_keeps_raw_sizes_in_evidence():
    """Сырые цифры — в evidence, не в теле сообщения (спека §5)."""
    n = to_notifications(5_000_000_000, _tables())[0]

    assert any("detector_signal_price_snapshots=3.08ГБ" in e for e in n.evidence)
    assert any("14878674" in e for e in n.evidence)


def test_report_survives_empty_table_list():
    """Пустой список таблиц не должен ронять отчёт — общий размер БД всё ещё факт."""
    n = to_notifications(5_000_000_000, [])[0]

    assert "5.00 ГБ" in " ".join(n.what_happened)
    assert n.evidence == ()


@pytest.mark.asyncio
async def test_run_report_goes_through_bus_only():
    """Риск №5 §10: миграция атомарна — прямой отправки в TG больше нет."""
    with (
        patch("app.jobs.db_size_report.fetchval", new=AsyncMock(return_value=5_000_000_000)),
        patch("app.jobs.db_size_report.fetch", new=AsyncMock(return_value=_tables())),
        patch("app.jobs.db_size_report.publish", new=AsyncMock()) as m_publish,
    ):
        from app.jobs.db_size_report import run_db_size_report

        await run_db_size_report()

    m_publish.assert_awaited_once()
    assert m_publish.await_args.kwargs["actor"] == "db_size_report"
