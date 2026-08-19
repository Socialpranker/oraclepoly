"""Task 2 (audit Phase 2): live-сигналы обрабатываются первыми в тике.

_FETCH_FRESH_SIGNALS_SQL без ORDER BY → live D3 ждёт за paper-симуляциями
(инцидент D38: 12-21 ордеров/мин в общем цикле). Тест — текстовая проверка
SQL-константы (без БД): ORDER BY должен идти после WHERE-блока и приоритизировать
detector_state.stage = 'live'.

Task C (anti-selection funnel fix, 2026-07-22): edge-ranking внутри stage —
лучшие сигналы конкурируют за лимит-слоты (daily_loss/max_open) первыми,
вместо FIFO по generated_at. edge = raw confidence − стоимость входа нашей
стороны; ds.generated_at остаётся tiebreaker.
"""

from app.services.detector_signal_service import _FETCH_FRESH_SIGNALS_SQL


def test_fresh_signals_prioritize_live():
    sql = _FETCH_FRESH_SIGNALS_SQL.lower()
    assert "order by (dst.stage = 'live') desc" in sql
    assert sql.index("order by") > sql.index("where")


def test_fresh_signals_ranked_by_edge_within_stage():
    """При равном stage сигнал с большим edge (confidence − entry_price
    нашей стороны) идёт первым; generated_at — tiebreaker.

    NULLS LAST — defensive (колонки сейчас NOT NULL, миграция
    20260430141632, но Postgres default для DESC — NULLS FIRST: без явного
    NULLS LAST будущий nullable edge обошёл бы всех и попал в лимит-слот
    первым — ровно anti-selection, который этот таск чинит)."""
    # Проверка текста SQL: ORDER BY содержит edge-выражение с NULLS LAST
    # между stage-сортировкой и generated_at ASC.
    assert "(dst.stage = 'live') DESC" in _FETCH_FRESH_SIGNALS_SQL
    edge_pos = _FETCH_FRESH_SIGNALS_SQL.index("GREATEST(ds.confidence")
    nulls_last_pos = _FETCH_FRESH_SIGNALS_SQL.index("DESC NULLS LAST")
    ts_pos = _FETCH_FRESH_SIGNALS_SQL.index("ds.generated_at ASC")
    assert edge_pos < nulls_last_pos < ts_pos
