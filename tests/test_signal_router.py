"""Signal Router: dedup по market_id внутри одного source."""


def _signal(
    market_id: str,
    side: str = "NO",
    entry_price: float = 0.45,
    category: str = "Sports",
    signal_source: str = "oracle",
    **kw,
) -> dict:
    return {
        "market_id": market_id,
        "side": side,
        "entry_price": entry_price,
        "category": category,
        "signal_source": signal_source,
        "oracle_score": kw.get("oracle_score", 0.40),
        "market_price": kw.get("market_price", 1.0 - entry_price),
        "divergence": kw.get("divergence", -0.05),
        "ev": kw.get("ev", 1.0),
        "volume_24h": kw.get("volume_24h", 5000),
        **kw,
    }


class TestDedupSignals:
    def test_same_market_keeps_highest_ev(self):
        from app.core.signal_router import dedup_signals

        signals = [_signal("m1", ev=0.5), _signal("m1", ev=1.0)]
        result = dedup_signals(signals)
        assert len(result) == 1
        assert result[0]["ev"] == 1.0

    def test_different_markets_both_kept(self):
        from app.core.signal_router import dedup_signals

        result = dedup_signals([_signal("m1"), _signal("m2")])
        assert len(result) == 2

    def test_empty_input(self):
        from app.core.signal_router import dedup_signals

        assert dedup_signals([]) == []

    def test_missing_market_id_skipped(self):
        from app.core.signal_router import dedup_signals

        assert dedup_signals([{"ev": 1.0}]) == []

    def test_three_dupes_keeps_best(self):
        from app.core.signal_router import dedup_signals

        signals = [_signal("m1", ev=0.5), _signal("m1", ev=0.8), _signal("m1", ev=1.0)]
        result = dedup_signals(signals)
        assert len(result) == 1
        assert result[0]["ev"] == 1.0
