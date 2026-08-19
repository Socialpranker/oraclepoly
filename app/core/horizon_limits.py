"""Global horizon limits for signal emission and execution.

Все детекторы и execution gate используют MAX_HORIZON_DAYS как hard cap
на время до резолва маркета. Превышение → сигнал не эмитится / ордер
не размещается.

SSoT: импортировать MAX_HORIZON в Python (timedelta) или использовать
MAX_HORIZON_SQL_INTERVAL в SQL запросах.
"""

from __future__ import annotations

from datetime import timedelta

MAX_HORIZON_DAYS: int = 30
MAX_HORIZON: timedelta = timedelta(days=MAX_HORIZON_DAYS)
MAX_HORIZON_SQL_INTERVAL: str = f"INTERVAL '{MAX_HORIZON_DAYS} days'"
