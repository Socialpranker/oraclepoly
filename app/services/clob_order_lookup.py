"""Поиск нашего ордера на CLOB после двусмысленного сбоя POST (timeout после принятия сервером).
Используется v2_execution (place except) и v2_execution_resolve (реаниматор intent).

py_clob_client_v2.ClobClient.get_open_orders(params: OpenOrderParams, ...) → list[dict]
бьёт в GET /data/orders (Polymarket CLOB "Get Active Orders"). Ответ — сырые dict'ы с
полями id / asset_id / market / side / status / created_at / original_size / price
(см. client.py, clob_types.py). Фильтруем server-side через OpenOrderParams(asset_id=token_id).

Только asset_id матчинг недостаточен: на этом же token_id может уже висеть чужой
resting ордер (например maker GTC от предыдущего сигнала), и honest-сверка ошибочно
припишет его текущей неудачной попытке (double-counted exposure, битый PnL при
резолве). Поэтому дополнительно сверяем expected_size (в пределах tolerance) и
not_before (ордер должен быть создан не раньше момента вызова _place_order).
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import structlog

logger = structlog.get_logger()


def _parse_created_at(raw) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


async def find_our_open_order(
    client,
    token_id: str,
    *,
    expected_size: float | None = None,
    size_tolerance_pct: float = 0.05,
    not_before: datetime | None = None,
) -> str | None:
    """Вернуть order_id самого свежего открытого ордера по token_id, либо None.

    expected_size/size_tolerance_pct и not_before сужают матчинг до ордера, который
    мы реально пытались разместить — иначе неродственный resting-ордер на том же
    token_id ложно засчитается как "наш".
    """
    try:
        from py_clob_client_v2 import OpenOrderParams

        orders = await asyncio.to_thread(client.get_open_orders, OpenOrderParams(asset_id=token_id))
        for o in sorted(orders or [], key=lambda x: x.get("created_at", ""), reverse=True):
            if o.get("asset_id") != token_id:
                continue
            if not_before is not None:
                created_at = _parse_created_at(o.get("created_at"))
                if created_at is None or created_at < not_before:
                    continue
            if expected_size is not None and expected_size > 0:
                order_size = float(o.get("original_size") or 0)
                if order_size <= 0:
                    continue
                rel_delta = abs(order_size - expected_size) / expected_size
                if rel_delta > size_tolerance_pct:
                    continue
            return o.get("id")
    except Exception as e:
        logger.warning("clob_order_lookup_failed", token_id=token_id, error=str(e)[:200])
    return None
