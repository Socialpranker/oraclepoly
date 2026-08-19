"""Repository для trade_signals — запись match'ей стратегий."""

from __future__ import annotations

import json

import structlog

from app.services.database import fetch, fetchrow

logger = structlog.get_logger()

# Колонки trade_signals для INSERT
_COLUMNS = [
    "strategy_key",
    "market_id",
    "event_id",
    "side",
    "entry_price",
    "oracle_score",
    "market_price",
    "divergence",
    "ev",
    "whale_contribution_raw",
    "sm_registry_raw",
    "whale_tx_count",
    "micro_factor_raw",
    "z_posterior",
    "news_contribution_raw",
    "volume_24h",
    "category",
    "end_date",
    "tier",
    "expected_accuracy",
    "question",
    "signal_source",
    "liquidity_meta",
]

_PLACEHOLDERS = ", ".join(f"${i + 1}" for i in range(len(_COLUMNS)))
_INSERT_SQL = f"""
    INSERT INTO trade_signals ({", ".join(_COLUMNS)})
    VALUES ({_PLACEHOLDERS})
    ON CONFLICT (strategy_key, market_id, snapshot_date) DO NOTHING
    RETURNING id
"""


class TradeSignalRepository:
    """CRUD для trade_signals."""

    async def record_signal(self, data: dict) -> str | None:
        """Записать сигнал. Возвращает UUID или None при дубликате."""
        values = []
        for c in _COLUMNS:
            v = data.get(c)
            # JSONB-колонки требуют сериализации dict → str для asyncpg
            if c == "liquidity_meta":
                v = json.dumps(v if isinstance(v, dict) else {})
            values.append(v)
        row = await fetchrow(_INSERT_SQL, *values)
        return str(row["id"]) if row else None

    async def get_signals(
        self,
        strategy_key: str,
        days: int = 30,
        category: str | None = None,
    ) -> list[dict]:
        """Список сигналов с фильтрами."""
        conditions = ["strategy_key = $1", "snapshot_date >= CURRENT_DATE - $2::int"]
        params: list = [strategy_key, days]

        if category:
            params.append(category)
            conditions.append(f"category = ${len(params)}")

        where = " AND ".join(conditions)
        rows = await fetch(
            f"SELECT * FROM trade_signals WHERE {where} ORDER BY created_at DESC",
            *params,
        )
        return [dict(r) for r in rows]

    async def get_backtest_summary(self, strategy_key: str) -> dict:
        """Агрегированные метрики: WR, wins, resolved через JOIN."""
        rows = await fetch(
            """
            SELECT
                ts.strategy_key,
                COUNT(*) AS total_signals,
                COUNT(*) FILTER (WHERE r.outcome IS NOT NULL) AS resolved,
                COUNT(*) FILTER (
                    WHERE r.outcome IS NOT NULL
                    AND (
                        (ts.side = 'YES' AND r.outcome = 'yes')
                        OR (ts.side = 'NO' AND r.outcome = 'no')
                    )
                ) AS wins,
                ROUND(100.0 * COUNT(*) FILTER (
                    WHERE r.outcome IS NOT NULL
                    AND (
                        (ts.side = 'YES' AND r.outcome = 'yes')
                        OR (ts.side = 'NO' AND r.outcome = 'no')
                    )
                ) / NULLIF(COUNT(*) FILTER (WHERE r.outcome IS NOT NULL), 0), 1) AS win_rate
            FROM trade_signals ts
            LEFT JOIN mv_market_resolved_outcome r ON r.market_id = ts.market_id
            WHERE ts.strategy_key = $1
            GROUP BY ts.strategy_key
            """,
            strategy_key,
        )
        if rows:
            return dict(rows[0])
        return {"strategy_key": strategy_key, "total_signals": 0, "resolved": 0, "wins": 0, "win_rate": None}
