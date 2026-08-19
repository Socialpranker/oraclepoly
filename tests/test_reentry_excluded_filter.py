"""Структурные тесты: re-entry фильтр консистентен во всех virtual-агрегатах.

Контекст (standalone_virtual_pnl_inflation_2026-06-04): re-entry позиции
STANDALONE раздували и косметический дашборд, и ПРОМОУТ-ГЕЙТ (+$41K фантома vs
+$781 честно). Помеченные backfill'ом (meta.reentry_excluded=true) позиции должны
исключаться ВЕЗДЕ, где суммируется virtual P&L — иначе гейт продолжит видеть
фантом и риск промоушена убыточного детектора в live остаётся.

Эти тесты — guard против регрессии: если кто-то уберёт фильтр из одного из 4
агрегатов, тест упадёт. Реальную фильтрацию (на данных) проверяет прод-verify
после backfill (см. план), т.к. агрегаты бьют по живой БД.

Предикат (единый): COALESCE((meta->>'reentry_excluded')::bool, false) = false
"""

from __future__ import annotations

import re

REENTRY_PREDICATE_RE = re.compile(
    r"COALESCE\(\s*\(\s*\w*\.?meta->>'reentry_excluded'\s*\)::bool\s*,\s*false\s*\)\s*=\s*false"
)


def _has_reentry_filter(sql: str) -> bool:
    return bool(REENTRY_PREDICATE_RE.search(sql))


def test_gate_metrics_sql_excludes_reentry():
    """ПРОМОУТ-ГЕЙТ: detector_metrics._VIRTUAL_METRICS_7D_SQL — самый важный.
    Без фильтра гейт видит фантомные re-entry → риск промоушена в live."""
    from app.services.detector_metrics import _VIRTUAL_METRICS_7D_SQL

    assert _has_reentry_filter(_VIRTUAL_METRICS_7D_SQL), (
        "Промоут-гейт SQL должен исключать meta.reentry_excluded"
    )


def test_daily_summary_job_sql_excludes_reentry():
    """TG daily summary job."""
    from app.jobs.daily_summary import _DAILY_ACTIVITY_SQL

    assert _has_reentry_filter(_DAILY_ACTIVITY_SQL)


def test_daily_summary_endpoint_sql_excludes_reentry():
    """Дашборд /v2/daily_summary endpoint (inline SQL в bot.py).

    SQL не вынесен в константу, поэтому читаем исходник функции через inspect.
    """
    import inspect

    from app.api import bot

    src = inspect.getsource(bot.bot_v2_daily_summary)
    assert _has_reentry_filter(src), "daily_summary endpoint должен исключать re-entry"


def test_pnl_by_mode_sql_excludes_reentry():
    """Per-detector dashboard P&L (DetectorLifecycleCard) — _PNL_BY_MODE_7D_SQL."""
    from app.services.detector_metrics import _PNL_BY_MODE_7D_SQL

    assert _has_reentry_filter(_PNL_BY_MODE_7D_SQL)


def test_virtual_fingerprints_sql_excludes_reentry():
    """avg_tp_pnl/avg_sl_pnl — вход honest promotion gates v2. Re-entry искажает."""
    from app.services.detector_metrics import _VIRTUAL_FINGERPRINTS_30D_SQL

    assert _has_reentry_filter(_VIRTUAL_FINGERPRINTS_30D_SQL)


def test_settle_velocity_sqls_exclude_reentry():
    """Settle-velocity распределение — re-entry (много быстрых settled) скашивает."""
    from app.api.bot import _SETTLE_VELOCITY_SQL, _SETTLE_VELOCITY_WEIGHTED_SQL

    assert _has_reentry_filter(_SETTLE_VELOCITY_SQL)
    assert _has_reentry_filter(_SETTLE_VELOCITY_WEIGHTED_SQL)


def test_activity_endpoints_exclude_reentry():
    """3 activity endpoint'а (per-day summary, day drill-down, calendar heat-map) —
    inline SQL, читаем через inspect. Все показывают per-day virtual P&L."""
    import inspect

    from app.api import bot

    for fn in (bot.activity_summary, bot.activity_day, bot.activity_calendar):
        src = inspect.getsource(fn)
        assert _has_reentry_filter(src), f"{fn.__name__} должен исключать re-entry"


def test_live_dashboard_equity_curve_excludes_reentry():
    """Equity curve virtual baseline (/v2/lifecycle/live_dashboard)."""
    import inspect

    from app.api import bot

    src = inspect.getsource(bot.bot_v2_lifecycle_live_dashboard)
    assert _has_reentry_filter(src)


def test_backfill_script_marks_only_reentries():
    """Backfill помечает ТОЛЬКО rn>1 (re-entry), первый вход (rn=1) не трогает,
    и пишет именно reentry_excluded=true."""
    from scripts.backfill_reentry_excluded import _FETCH_REENTRY_SQL, _UPDATE_SQL

    # Дедуп по (detector, event_id), помечаем повторы.
    assert "ROW_NUMBER() OVER (PARTITION BY detector, event_id ORDER BY created_at)" in _FETCH_REENTRY_SQL
    assert "WHERE rn > 1" in _FETCH_REENTRY_SQL
    # Только реальные позиции, не rejected/cancelled.
    assert "virtual_placed" in _FETCH_REENTRY_SQL
    assert "virtual_settled" in _FETCH_REENTRY_SQL
    assert "rejected" not in _FETCH_REENTRY_SQL
    # UPDATE добавляет ключ обратимо (meta || ), не перезаписывает весь meta.
    assert "reentry_excluded" in _UPDATE_SQL
    assert "||" in _UPDATE_SQL


def test_backfill_phantom_tp_sql_matches_guard_criterion():
    """Phantom-TP fetch SQL использует ТОТ ЖЕ критерий, что guard в position_manager:
    take_profit + hold≤20мин + exit на резолюц. экстремуме (≤0.02 OR ≥0.98).
    Помечает тем же reentry_excluded (фильтр уже везде) — не вводит новый маркер."""
    from scripts.backfill_reentry_excluded import _FETCH_PHANTOM_TP_SQL, _UPDATE_SQL

    assert "exit_reason = 'take_profit'" in _FETCH_PHANTOM_TP_SQL
    assert "/ 60.0 <= 20" in _FETCH_PHANTOM_TP_SQL  # hold ≤20 мин
    assert "exit_price <= 0.02" in _FETCH_PHANTOM_TP_SQL
    assert "exit_price >= 0.98" in _FETCH_PHANTOM_TP_SQL
    # Тот же UPDATE → тот же маркер reentry_excluded (0 нового кода в 11 фильтрах).
    assert "phantom_tp_excluded" not in _UPDATE_SQL
    assert "reentry_excluded" in _UPDATE_SQL
