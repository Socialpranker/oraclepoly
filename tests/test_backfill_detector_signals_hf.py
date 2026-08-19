"""Tests для scripts/research/backfill_detector_signals_hf.py.

Стратегия: тестируем pure helpers + adapters, не end-to-end DuckDB-flow
(тот требует canonical layer на проде, /mnt/data/canonical/).
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "research"
    / "backfill_detector_signals_hf.py"
)

_spec = importlib.util.spec_from_file_location("backfill_hf", SCRIPT)
backfill_hf = importlib.util.module_from_spec(_spec)
sys.modules["backfill_hf"] = backfill_hf  # required для @dataclass(frozen=True) inside module
_spec.loader.exec_module(backfill_hf)


# ── _resolve_outcome ─────────────────────────────────────────────────────


class TestResolveOutcome:
    def test_yes_won_yes_side_returns_win(self):
        assert backfill_hf._resolve_outcome(yes_won=1, side="YES") == "WIN"

    def test_yes_won_no_side_returns_loss(self):
        assert backfill_hf._resolve_outcome(yes_won=1, side="NO") == "LOSS"

    def test_no_won_yes_side_returns_loss(self):
        assert backfill_hf._resolve_outcome(yes_won=0, side="YES") == "LOSS"

    def test_no_won_no_side_returns_win(self):
        assert backfill_hf._resolve_outcome(yes_won=0, side="NO") == "WIN"

    def test_null_yes_won_returns_expired(self):
        assert backfill_hf._resolve_outcome(yes_won=None, side="YES") == "EXPIRED"
        assert backfill_hf._resolve_outcome(yes_won=None, side="NO") == "EXPIRED"


# ── _resolved_at ─────────────────────────────────────────────────────────


class TestResolvedAt:
    def test_aware_datetime_returns_as_is(self):
        end = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
        assert backfill_hf._resolved_at(end) == end

    def test_naive_datetime_becomes_utc(self):
        end = datetime(2026, 1, 15, 12, 0)
        res = backfill_hf._resolved_at(end)
        assert res.tzinfo is not None
        assert res.year == 2026
        assert res.hour == 12

    def test_none_falls_back_to_now(self):
        res = backfill_hf._resolved_at(None)
        assert isinstance(res, datetime)
        assert res.tzinfo is not None


# ── _row_to_market_event adapters ────────────────────────────────────────


class TestRowToMarketEvent:
    def _make_row(self, **overrides):
        base = {
            "market_id": "0xMARKET",
            "event_id": "evt-1",
            "category": "Sports",
            "event_title": "Some game",
            "yes_price": 0.85,
            "hours_to_resolve": 120.0,
            "horizon_days": 5.0,
            "lifetime_volume_usd": 25_000.0,
            "volume_24h": 800.0,
            "neg_risk": False,
            "question": "Will A beat B?",
            "price_min_asof": 0.08,
        }
        base.update(overrides)
        return base

    def test_d13_event_includes_question(self):
        spec = backfill_hf.SINGLE_MARKET_SPECS["D13"]
        me = backfill_hf._row_to_market_event(
            self._make_row(
                question="Will candidate A win?",
                category="Politics",
                hours_to_resolve=4.0,
                yes_price=0.62,
            ),
            spec,
        )
        assert me["question"] == "Will candidate A win?"
        assert me["current_price"] == 0.62


# ── Event-level detector inputs (D3/D29) ─────────────────────────────────


class TestEventLevelAdapters:
    """Smoke-тесты что event-level dict проходит через detect()."""

    def test_d3_neg_risk_arb_emits_on_sum_above_threshold(self):
        from app.detectors import d3_cross_mkt_arb

        # 3 markets with sum_yes=1.30 → D3 should emit NO on highest
        markets = [
            d3_cross_mkt_arb.EventMarket(market_id=f"0xM{i}", yes_price=p, token_id=None)
            for i, p in enumerate([0.55, 0.40, 0.35])
        ]
        signals = d3_cross_mkt_arb.detect(
            event_id="evt-d3",
            markets=markets,
            neg_risk=True,
            neg_risk_other=False,
            n_yes_winners=1,
        )
        assert len(signals) == 1
        assert signals[0].side == "NO"
        assert signals[0].market_id == "0xM0"  # highest yes

    def test_d29_basket_emits_on_sum_above_threshold(self):
        from app.detectors import d29_negrisk_basket_arb

        # 3 markets, sum_yes=1.15 → D29 emits, primary = lowest yes (= highest NO entry)
        basket_event = {
            "event_id": "evt-d29",
            "event_title": "Some event",
            "markets": [
                {"market_id": "0xA", "yes_price": 0.60, "clob_token_ids": None,
                 "volume_24h": 100.0, "end_date": None, "category": "Sports"},
                {"market_id": "0xB", "yes_price": 0.35, "clob_token_ids": None,
                 "volume_24h": 100.0, "end_date": None, "category": "Sports"},
                {"market_id": "0xC", "yes_price": 0.20, "clob_token_ids": None,
                 "volume_24h": 100.0, "end_date": None, "category": "Sports"},
            ],
        }
        sig = d29_negrisk_basket_arb.detect(basket_event)
        assert sig is not None
        assert sig.side == "NO"
        # D29 primary = market с min yes_price (где NO entry самая дорогая,
        # ведёт себя как "anchor" для basket)
        assert sig.market_id == "0xC"
        assert sig.confidence == 1.0  # mathematically guaranteed
