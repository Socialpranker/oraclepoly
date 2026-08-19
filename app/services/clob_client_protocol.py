"""Protocol interface для CLOB client — Phase 1 Stage 8.

Duck-typing parity между real `py_clob_client_v2.ClobClient` и
`FakeClobClient` (drop-in replacement для drill / CI / unit tests).

Используется через `app.services.clob_factory.make_clob_client()` который
читает `Settings.execution_mock_mode` и возвращает либо real, либо Fake.

Endpoints обязательные для mimic (5 main):
  - `create_and_post_order` — placement
  - `get_trades` — orphan detection / reconciliation
  - `cancel_order` — stale cancel
  - `get_order_book` — pre-trade book inspection
  - `get_balance_allowance` — bankroll check

См. docs/superpowers/plans/2026-05-risk-engine-hardening/08-live-drill.md
+ research/live-drill-tabletop-2026-05/report.md.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ClobClientProtocol(Protocol):
    """Duck-typing contract для CLOB client.

    Real `py_clob_client_v2.ClobClient` satisfies этот Protocol implicitly.
    `FakeClobClient` satisfies его explicitly через method signatures.

    `@runtime_checkable` позволяет `isinstance(obj, ClobClientProtocol)` —
    нужно для factory function dispatch.
    """

    def create_and_post_order(self, args: Any, order_type: str | None = None) -> dict[str, Any]:
        """Place new order. Returns dict с success/orderID/status/size/filled."""
        ...

    def get_trades(self, params: Any = None) -> list[dict[str, Any]]:
        """Fetch executed trades (for orphan detection / reconciliation)."""
        ...

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Cancel placed order. Returns dict с success/orderID/status."""
        ...

    def get_order_book(self, token_id: str) -> dict[str, Any]:
        """Fetch L2 order book. Returns dict с bids/asks/timestamp."""
        ...

    def get_balance_allowance(self, params: Any = None) -> dict[str, Any]:
        """Fetch wallet USDC balance + Polymarket allowance."""
        ...
