# tests/test_signal_helpers.py
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest


class TestDedupByEvent:
    def test_no_event_ids(self):
        """Все items без event_id проходят без дедупликации."""
        items = [{"name": "a"}, {"name": "b"}]
        from app.core.signal_helpers import dedup_by_event

        result = dedup_by_event(items, key_fn=lambda x: 0)
        assert len(result) == 2

    def test_dedup_same_event(self):
        """Из двух маркетов одного события — оставить с большим price."""
        items = [
            {"event_id": "ev1", "price": 0.3},
            {"event_id": "ev1", "price": 0.7},
            {"event_id": "ev2", "price": 0.5},
        ]
        from app.core.signal_helpers import dedup_by_event

        result = dedup_by_event(items, key_fn=lambda x: x["price"])
        assert len(result) == 2
        ev1_items = [r for r in result if r.get("event_id") == "ev1"]
        assert len(ev1_items) == 1
        assert ev1_items[0]["price"] == 0.7

    def test_no_event_id_kept(self):
        """Items без event_id не теряются при дедупликации."""
        items = [
            {"event_id": "ev1", "price": 0.3},
            {"event_id": "ev1", "price": 0.7},
            {"name": "orphan"},
        ]
        from app.core.signal_helpers import dedup_by_event

        result = dedup_by_event(items, key_fn=lambda x: x.get("price", 0))
        assert len(result) == 2
        orphans = [r for r in result if "name" in r]
        assert len(orphans) == 1
        assert orphans[0]["name"] == "orphan"


class TestComputeEV:
    @patch("app.core.signal_helpers.estimate_trading_fee", return_value=0.05)
    def test_positive_ev(self, mock_fee):
        from app.core.signal_helpers import compute_ev

        result = compute_ev(p_win=0.65, entry_price=0.40, stake=10.0, category="sports")
        assert result["gross_ev"] > 0
        assert result["net_ev"] > 0
        assert "ev_percent" in result
        assert "payout_if_win" in result

    @patch("app.core.signal_helpers.estimate_trading_fee", return_value=0.05)
    def test_negative_ev(self, mock_fee):
        from app.core.signal_helpers import compute_ev

        result = compute_ev(p_win=0.30, entry_price=0.90, stake=10.0, category="sports")
        assert result["gross_ev"] < 0
        assert result["net_ev"] < result["gross_ev"]

    @patch("app.core.signal_helpers.estimate_trading_fee", return_value=0.12)
    def test_fee_calculated(self, mock_fee):
        from app.core.signal_helpers import compute_ev

        result = compute_ev(p_win=0.60, entry_price=0.50, stake=10.0, category="crypto")
        assert result["est_fee"] == 0.12
        mock_fee.assert_called_once()
        args = mock_fee.call_args[0]
        # gross_notional = 10 / 0.50 = 20.0
        assert args[0] == pytest.approx(20.0)
        # market_price defaults to entry_price
        assert args[1] == pytest.approx(0.50)
        assert args[2] == "crypto"

    @patch("app.core.signal_helpers.estimate_trading_fee", return_value=0.08)
    def test_market_price_for_fee(self, mock_fee):
        from app.core.signal_helpers import compute_ev

        compute_ev(p_win=0.60, entry_price=0.50, stake=10.0, category="sports", market_price=0.55)
        args = mock_fee.call_args[0]
        assert args[1] == pytest.approx(0.55)

    @patch("app.core.signal_helpers.estimate_trading_fee", return_value=0.05)
    def test_passes_fee_schedule_through(self, mock_fee):
        """compute_ev пробрасывает fee_schedule в estimate_trading_fee.
        Регрессия для feeSchedule per-market подключения 2026-05-21."""
        from app.core.signal_helpers import compute_ev

        schedule = {"rate": 0.04, "exponent": 1, "takerOnly": True, "rebateRate": 0.25}
        compute_ev(
            p_win=0.60,
            entry_price=0.50,
            stake=10.0,
            category="politics",
            fee_schedule=schedule,
        )
        args = mock_fee.call_args[0]
        # 4-й positional аргумент — fee_schedule
        assert args[3] == schedule

    @patch("app.core.signal_helpers.estimate_trading_fee", return_value=0.05)
    def test_fee_schedule_default_none(self, mock_fee):
        """Без явного fee_schedule — пробрасываем None (backwards-compat)."""
        from app.core.signal_helpers import compute_ev

        compute_ev(p_win=0.60, entry_price=0.50, stake=10.0, category="politics")
        args = mock_fee.call_args[0]
        assert args[3] is None


class TestParseHorizonDays:
    def test_string_dates(self):
        from app.core.signal_helpers import parse_horizon_days

        end = "2026-04-20T00:00:00Z"
        start = "2026-04-10T00:00:00Z"
        result = parse_horizon_days(end, start)
        assert result == pytest.approx(10.0)

    def test_none_inputs(self):
        from app.core.signal_helpers import parse_horizon_days

        assert parse_horizon_days(None, "2026-04-10T00:00:00Z") is None
        assert parse_horizon_days("2026-04-20T00:00:00Z", None) is None
        assert parse_horizon_days(None, None) is None

    def test_negative_clamped(self):
        from app.core.signal_helpers import parse_horizon_days

        # end < start — должно вернуть 0.01
        result = parse_horizon_days("2026-04-01T00:00:00Z", "2026-04-10T00:00:00Z")
        assert result == pytest.approx(0.01)

    def test_datetime_inputs(self):
        from app.core.signal_helpers import parse_horizon_days

        end = datetime(2026, 4, 20, tzinfo=UTC)
        start = datetime(2026, 4, 10, tzinfo=UTC)
        result = parse_horizon_days(end, start)
        assert result == pytest.approx(10.0)


class TestBuildSignalDict:
    def test_all_fields_present(self):
        from app.core.signal_helpers import build_signal_dict

        market = {
            "market_id": "m1",
            "event_id": "e1",
            "question": "Will X happen?",
            "category": "sports",
            "volume_24h": 12345.0,
            "end_date": "2026-04-20",
            "whale_tx_count": 3,
            "whale_contribution_raw": 0.12,
            "z_posterior": 0.55,
            "sm_registry_raw": 0.1,
            "micro_factor_raw": 0.0,
            "news_contribution_raw": 0.05,
            "created_at": "2026-04-10",
        }
        ev_result = {
            "net_ev": 1.23,
            "ev_percent": 12.3,
            "est_fee": 0.05,
            "p_win": 0.65,
            "payout_if_win": 15.0,
            "gross_ev": 1.28,
        }
        result = build_signal_dict(
            market=market,
            side="NO",
            entry_price=0.4,
            market_price=0.45,
            ev_result=ev_result,
            stake=10.0,
            signal_source="oracle",
        )

        assert result["market_id"] == "m1"
        assert result["event_id"] == "e1"
        assert result["side"] == "NO"
        assert result["market_price"] == 0.45
        assert result["entry_price"] == 0.4
        assert result["stake"] == 10.0
        assert result["signal_source"] == "oracle"
        assert result["ev"] == 1.23
        assert result["ev_percent"] == 12.3
        assert result["est_fee"] == 0.05
        assert result["p_win"] == 0.65
        assert result["payout_if_win"] == 15.0
        assert result["whale_tx_count"] == 3
        assert result["snapshot_created_at"] == "2026-04-10"
        assert result["category"] == "sports"

    def test_extra_fields_merged(self):
        from app.core.signal_helpers import build_signal_dict

        market = {"id": "m2", "question": "Q?", "category": "crypto", "created_at": "2026-04-10"}
        ev_result = {
            "net_ev": 0.5,
            "ev_percent": 5.0,
            "est_fee": 0.02,
            "p_win": 0.6,
            "payout_if_win": 10.0,
            "gross_ev": 0.52,
        }
        result = build_signal_dict(
            market=market,
            side="YES",
            entry_price=0.5,
            market_price=0.5,
            ev_result=ev_result,
            stake=10.0,
            signal_source="oracle",
            extra={"oracle_score": 0.72, "divergence": 0.15, "snapshot_price": 0.55},
        )

        assert result["market_id"] == "m2"
        assert result["oracle_score"] == 0.72
        assert result["divergence"] == 0.15
        assert result["snapshot_price"] == 0.55

    def test_missing_numeric_fields_default(self):
        from app.core.signal_helpers import build_signal_dict

        market = {"id": "m3", "question": "Q?", "category": "other", "created_at": "2026-04-10"}
        ev_result = {
            "net_ev": 0.1,
            "ev_percent": 1.0,
            "est_fee": 0.01,
            "p_win": 0.55,
            "payout_if_win": 8.0,
            "gross_ev": 0.11,
        }
        result = build_signal_dict(
            market=market,
            side="NO",
            entry_price=0.4,
            market_price=0.45,
            ev_result=ev_result,
            stake=10.0,
            signal_source="market_baseline",
        )

        assert result["whale_tx_count"] == 0
        assert result["whale_contribution_raw"] == 0.0
        assert result["z_posterior"] == 0.0
        assert result["volume_24h"] == 0.0


def test_build_signal_dict_includes_liquidity_meta():
    """liquidity_meta из extra должен попасть в результирующий dict."""
    from app.core.signal_helpers import build_signal_dict

    market = {
        "market_id": "m1",
        "event_id": None,
        "question": "Test?",
        "category": "Sports",
        "volume_24h": 500.0,
        "end_date": None,
    }
    ev_result = {
        "net_ev": 1.5,
        "ev_percent": 15.0,
        "est_fee": 0.1,
        "p_win": 0.6,
        "payout_if_win": 5.0,
    }
    liquidity_meta = {"tier": "thin", "volume_24h": 500.0, "best_book_depth": None}

    signal = build_signal_dict(
        market=market,
        side="YES",
        entry_price=0.5,
        market_price=0.5,
        ev_result=ev_result,
        stake=10.0,
        signal_source="oracle",
        extra={"liquidity_meta": liquidity_meta},
    )

    assert signal["liquidity_meta"] == liquidity_meta
    assert signal["liquidity_meta"]["tier"] == "thin"
