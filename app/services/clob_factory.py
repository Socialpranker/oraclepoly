"""CLOB client factory — Phase 1 Stage 8.

Switches между real `py_clob_client_v2.ClobClient` и `FakeClobClient` по
`Settings.execution_mock_mode`. Используется везде где раньше был direct
import real client.

Pattern: factory function (Hummingbot `_paper_trade` suffix style).
"""

from __future__ import annotations

from app.core.config import get_settings
from app.services.clob_client_protocol import ClobClientProtocol
from app.services.fake_clob_client import FakeClobClient


def make_clob_client() -> ClobClientProtocol:
    """Return CLOB client per current Settings.

    EXECUTION_MOCK_MODE=true → FakeClobClient (stateful, deterministic if seed set).
    EXECUTION_MOCK_MODE=false → real `py_clob_client_v2.ClobClient`.

    Real client lazily imported чтобы тесты не требовали py_clob_client_v2 installed.
    """
    settings = get_settings()
    if settings.execution_mock_mode:
        return FakeClobClient(
            latency_ms=(settings.mock_latency_min_ms, settings.mock_latency_max_ms),
            partial_fill_rate=settings.mock_partial_rate,
            failure_rate=settings.mock_failure_rate,
            seed=settings.mock_seed,
        )

    # Real client. Import lazy to avoid require py_clob_client_v2 in test env.
    from app.services.wallet_resolver import build_clob_client

    return build_clob_client()
