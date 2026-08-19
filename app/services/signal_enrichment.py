"""Обогащение live signals полями из raw oracle snapshots."""

from __future__ import annotations

_ENRICH_FIELDS = {
    "whale_contribution_raw": 0,
    "sm_registry_raw": 0,
    "whale_tx_count": 0,
    "micro_factor_raw": 0,
    "z_posterior": 0,
}


def enrich_signals(signals: list[dict], snap_lookup: dict[str, dict]) -> list[dict]:
    """Обогатить signals полями из raw snapshots для cluster classifier и trade_signals.

    Мутирует signals in-place и возвращает их же.
    """
    for signal in signals:
        raw = snap_lookup.get(signal.get("market_id", ""))
        for field, default in _ENRICH_FIELDS.items():
            signal[field] = raw.get(field, default) if raw else default
        # created_at: из raw snapshot, fallback на snapshot_created_at из signal
        raw_created = raw.get("created_at") if raw else None
        signal["created_at"] = raw_created or signal.get("snapshot_created_at")
    return signals
