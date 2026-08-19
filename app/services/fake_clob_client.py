"""Fake CLOB client — Phase 1 Stage 8.

Drop-in replacement для `py_clob_client_v2.ClobClient`. Stateful in-memory
implementation для drill / CI / unit tests. НЕ для performance estimation
strategy — Honest Baseline + virtual stage отвечают за это (rule #10).

Design:
- **Stateful**: dict[order_id, OrderState] для full lifecycle (placed →
  filled → settled).
- **Latency simulation**: `asyncio.sleep(uniform(50, 300) ms)` для mimic
  real Polymarket API roundtrip.
- **Partial fills**: `partial_fill_rate=0.3` → 30% orders fill 10-90% size.
- **Failure injection**: `failure_rate=0.0` controlled — для chaos drills.
- **Deterministic seed**: `seed=N` гарантирует reproducible runs (Hummingbot
  pattern).

Используется через `clob_factory.make_clob_client()` который switches между
real client и Fake по `Settings.execution_mock_mode`.

См. research/live-drill-tabletop-2026-05/report.md §Recommended implementation.
"""

from __future__ import annotations

import asyncio
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class OrderState:
    """Internal Fake state для один order."""

    order_id: str
    token_id: str
    side: str  # "BUY" / "SELL"
    size: float
    price: float
    filled: float
    status: str  # MATCHED / PARTIAL / CANCELLED / PLACED
    created_ts: float
    last_update_ts: float


@dataclass
class TradeRecord:
    """Internal Fake trade (для get_trades)."""

    trade_id: str
    order_id: str
    token_id: str
    side: str
    size: float
    price: float
    trader_side: str  # "TAKER" / "MAKER"
    ts: float
    maker_orders: list[dict[str, Any]] = field(default_factory=list)


class FakeClobClient:
    """Stateful in-memory CLOB client для drill / CI.

    Args:
        latency_ms: (min, max) network latency range в миллисекундах.
        partial_fill_rate: вероятность partial fill (0.0..1.0).
        failure_rate: вероятность API call failure (0.0..1.0).
        seed: deterministic seed для reproducible runs. None=non-deterministic.
        initial_balance_usdc: starting wallet balance.

    Conformance: satisfies `ClobClientProtocol` interface implicitly.
    """

    def __init__(
        self,
        *,
        latency_ms: tuple[int, int] = (50, 300),
        partial_fill_rate: float = 0.3,
        failure_rate: float = 0.0,
        seed: int | None = None,
        initial_balance_usdc: float = 10_000.0,
    ) -> None:
        self._latency_ms = latency_ms
        self._partial_fill_rate = partial_fill_rate
        self._failure_rate = failure_rate
        self._rng = random.Random(seed)
        self._orders: dict[str, OrderState] = {}
        self._trades: list[TradeRecord] = []
        self._balance_usdc = initial_balance_usdc
        self._frozen_usdc = 0.0
        # Synthetic order books (static, can be enhanced для drill scenarios).
        self._books: dict[str, dict[str, Any]] = {}

    # --- Simulation helpers ---

    async def _simulate_network(self) -> None:
        """Inject latency + optional failure."""
        latency_sec = self._rng.uniform(*self._latency_ms) / 1000.0
        await asyncio.sleep(latency_sec)
        if self._rng.random() < self._failure_rate:
            raise TimeoutError(f"injected_failure (rate={self._failure_rate})")

    def _generate_order_id(self) -> str:
        # uuid4 seeded через rng для deterministic runs
        if self._rng is not None:
            # rng-derived UUID для deterministic seed behaviour
            bytes_seed = bytes(self._rng.getrandbits(8) for _ in range(16))
            return str(uuid.UUID(bytes=bytes_seed, version=4))
        return str(uuid.uuid4())

    # --- ClobClientProtocol methods ---

    async def create_and_post_order(self, args: Any, order_type: str | None = None) -> dict[str, Any]:
        """Place new order. Stateful: persist в _orders, optionally fill."""
        await self._simulate_network()

        # Extract args (duck-typed: real py-clob args либо dict).
        size = float(getattr(args, "size", None) or args.get("size", 0))
        price = float(getattr(args, "price", None) or args.get("price", 0))
        side = str(getattr(args, "side", None) or args.get("side", "BUY"))
        token_id = str(getattr(args, "token_id", None) or args.get("token_id", ""))

        if size <= 0 or price <= 0 or price >= 1:
            return {"success": False, "error": "invalid_args"}

        order_id = self._generate_order_id()
        now = time.time()

        # Decide fill state.
        is_partial = self._rng.random() < self._partial_fill_rate
        if is_partial:
            filled_frac = self._rng.uniform(0.1, 0.9)
            filled = size * filled_frac
            status = "PARTIAL"
        else:
            filled = size
            status = "MATCHED"

        order = OrderState(
            order_id=order_id,
            token_id=token_id,
            side=side,
            size=size,
            price=price,
            filled=filled,
            status=status,
            created_ts=now,
            last_update_ts=now,
        )
        self._orders[order_id] = order

        # Trade record (для get_trades reconciliation).
        if filled > 0:
            trade = TradeRecord(
                trade_id=self._generate_order_id(),
                order_id=order_id,
                token_id=token_id,
                side=side,
                size=filled,
                price=price,
                trader_side="TAKER" if order_type == "FOK" else "MAKER",
                ts=now,
                maker_orders=([{"order_id": order_id, "size": str(filled)}] if order_type != "FOK" else []),
            )
            self._trades.append(trade)

        # Update balance (frozen stake).
        stake = filled * price
        self._frozen_usdc += stake
        self._balance_usdc = max(0.0, self._balance_usdc - stake)

        return {
            "success": True,
            "orderID": order_id,
            "status": status,
            "size": size,
            "filled": filled,
            "price": price,
        }

    async def get_trades(self, params: Any = None) -> list[dict[str, Any]]:
        """Fetch executed trades. Optionally filter by params."""
        await self._simulate_network()
        # Simple: return all trades. Real client supports filters (market, time).
        return [
            {
                "id": t.trade_id,
                "taker_order_id": t.order_id if t.trader_side == "TAKER" else None,
                "maker_orders": t.maker_orders,
                "trader_side": t.trader_side,
                "size": str(t.size),
                "price": str(t.price),
                "token_id": t.token_id,
                "side": t.side,
                "timestamp": t.ts,
            }
            for t in self._trades
        ]

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Cancel placed order. Real client returns success/orderID/status."""
        await self._simulate_network()
        order = self._orders.get(order_id)
        if order is None:
            return {"success": False, "error": "order_not_found"}
        if order.status in ("MATCHED", "CANCELLED"):
            return {"success": False, "error": f"cannot_cancel_status_{order.status}"}

        # Unfreeze remaining size.
        unfilled = order.size - order.filled
        refund = unfilled * order.price
        self._frozen_usdc = max(0.0, self._frozen_usdc - refund)
        self._balance_usdc += refund

        order.status = "CANCELLED"
        order.last_update_ts = time.time()
        return {
            "success": True,
            "orderID": order_id,
            "status": "CANCELLED",
            "cancelled_size": unfilled,
        }

    async def get_order_book(self, token_id: str) -> dict[str, Any]:
        """Synthetic order book. Static unless заменили через `seed_book()`."""
        await self._simulate_network()
        book = self._books.get(token_id)
        if book is not None:
            return book
        # Default synthetic: tight book around 0.50.
        return {
            "token_id": token_id,
            "bids": [
                {"price": "0.495", "size": "1000"},
                {"price": "0.490", "size": "5000"},
                {"price": "0.485", "size": "10000"},
            ],
            "asks": [
                {"price": "0.505", "size": "1000"},
                {"price": "0.510", "size": "5000"},
                {"price": "0.515", "size": "10000"},
            ],
            "timestamp": time.time(),
        }

    async def get_balance_allowance(self, params: Any = None) -> dict[str, Any]:
        """Fetch USDC balance + Polymarket allowance."""
        await self._simulate_network()
        return {
            "balance": str(self._balance_usdc),
            "allowance": str(self._balance_usdc + self._frozen_usdc),
            "asset_type": "COLLATERAL",
        }

    # --- Drill helpers (not in real Protocol) ---

    def seed_book(self, token_id: str, book: dict[str, Any]) -> None:
        """Override synthetic book для конкретного token_id (drill scenario setup)."""
        self._books[token_id] = book

    def set_failure_rate(self, rate: float) -> None:
        """Adjust failure injection mid-drill (chaos scenarios)."""
        self._failure_rate = max(0.0, min(1.0, rate))

    def get_open_orders(self) -> list[OrderState]:
        """Inspect current state (для drill assertions)."""
        return [o for o in self._orders.values() if o.status in ("PLACED", "PARTIAL")]

    def reset_state(self) -> None:
        """Clear all orders/trades. Полный reset между drill scenarios."""
        self._orders.clear()
        self._trades.clear()
        self._frozen_usdc = 0.0
