"""Unit тесты для app.jobs.db_size_monitor — конверт уведомлений (спека §3).

Тестов у джобы не было вообще: она молча слала HTML прямо в TG. Первое, что
здесь проверяется, — риск №1 §10 (факт доставки), потому что «монитор, который
перестал слать» выглядит ровно как «всё в порядке».
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.analytics.notification import Severity
from app.jobs.db_size_monitor import to_notifications


def _alert(table="markets", total_mb=1300, threshold_mb=1200):
    return {"table": table, "total_mb": total_mb, "threshold_mb": threshold_mb}


def test_no_alerts_no_notification():
    """Порог не превышен — молчим. Иначе ACTION_REQUIRED каждый день (§4: ≤2/день)."""
    assert to_notifications([]) == []


def test_alert_is_action_required_with_next_step():
    n = to_notifications([_alert()])[0]

    assert n.severity == Severity.ACTION_REQUIRED
    assert n.subject == "infra:db_size"
    assert n.what_to_do is not None


def test_several_tables_merge_into_one_message():
    """Причина общая (чистка отстаёт) и действие одно — три сообщения про три
    таблицы это шум, от которого уводит §4."""
    ns = to_notifications([_alert("markets"), _alert("whales", 400, 350), _alert("transactions", 2100, 2048)])

    assert len(ns) == 1
    body = " ".join(ns[0].what_happened)
    assert "markets" in body
    assert "whales" in body
    assert "transactions" in body


def test_never_invents_money_figure():
    """Спека §3: цены бездействия в мегабайтах не существует — выдумывать запрещено."""
    assert to_notifications([_alert()])[0].what_if_ignored is None


def test_numbers_are_named_with_meaning():
    """Спека §1 п.2: цифра без расшифровки — жаргон. Порог назван вслух."""
    body = " ".join(to_notifications([_alert("markets", 1300, 1200)])[0].what_happened)

    assert "1300" in body
    assert "1200" in body


@pytest.mark.asyncio
async def test_run_publishes_when_threshold_exceeded():
    """Риск №1 §10: превышение обязано ДОЙТИ до владельца."""
    rows = [
        {
            "table_name": "markets",
            "total_bytes": 1300 * 1024 * 1024,
            "data_bytes": 1000 * 1024 * 1024,
            "index_bytes": 300 * 1024 * 1024,
        }
    ]
    with (
        patch("app.jobs.db_size_monitor.fetch", new=AsyncMock(return_value=rows)),
        patch("app.jobs.db_size_monitor.publish", new=AsyncMock()) as m_publish,
    ):
        from app.jobs.db_size_monitor import run_db_size_monitor

        result = await run_db_size_monitor()

    m_publish.assert_awaited_once()
    assert m_publish.await_args.kwargs["actor"] == "db_size_monitor"
    assert len(result["alerts"]) == 1


@pytest.mark.asyncio
async def test_run_stays_silent_below_threshold():
    """Ниже порога шина не дёргается вообще — иначе cooldown съест реальный алерт."""
    rows = [
        {
            "table_name": "markets",
            "total_bytes": 100 * 1024 * 1024,
            "data_bytes": 90 * 1024 * 1024,
            "index_bytes": 10 * 1024 * 1024,
        }
    ]
    with (
        patch("app.jobs.db_size_monitor.fetch", new=AsyncMock(return_value=rows)),
        patch("app.jobs.db_size_monitor.publish", new=AsyncMock()) as m_publish,
    ):
        from app.jobs.db_size_monitor import run_db_size_monitor

        result = await run_db_size_monitor()

    m_publish.assert_not_awaited()
    assert result["alerts"] == []
