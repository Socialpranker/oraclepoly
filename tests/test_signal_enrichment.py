"""Тесты enrich_signals — обогащение live signals полями из raw snapshots."""

from __future__ import annotations

from app.services.signal_enrichment import enrich_signals


def test_enrich_copies_fields():
    signals = [{"market_id": "m1", "ev": 0.1, "category": "crypto", "end_date": "2026-04-10"}]
    snap_lookup = {
        "m1": {
            "whale_contribution_raw": 0.5,
            "sm_registry_raw": 0.2,
            "whale_tx_count": 3,
            "micro_factor_raw": -0.1,
            "z_posterior": -0.5,
            "news_contribution_raw": 0.0,
            "created_at": "2026-04-05",
        },
    }
    result = enrich_signals(signals, snap_lookup)
    assert result[0]["whale_contribution_raw"] == 0.5
    assert result[0]["category"] == "crypto"  # сохраняется из signal (не из enrich)
    assert result[0]["created_at"] == "2026-04-05"


def test_enrich_missing_snapshot_uses_defaults():
    signals = [{"market_id": "m_unknown", "ev": 0.1, "category": "Unknown"}]
    result = enrich_signals(signals, {})
    assert result[0]["whale_contribution_raw"] == 0
    assert result[0]["category"] == "Unknown"  # не перезаписывается enrichment


def test_enrich_created_at_fallback():
    signals = [{"market_id": "m1", "snapshot_created_at": "2026-04-01"}]
    snap_lookup = {"m1": {"created_at": None}}
    result = enrich_signals(signals, snap_lookup)
    assert result[0]["created_at"] == "2026-04-01"
