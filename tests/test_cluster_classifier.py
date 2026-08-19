"""Тесты ClusterClassifier v3 — lookup по верифицированным кластерным данным."""

from app.core.cluster_classifier import Tier, classify_snapshot


class TestClassifySnapshot:
    """Tier classification из snapshot dict."""

    def test_tier1_crypto_weak_registry(self):
        """Crypto 1-3d + weak + reg+ = Tier 1 (96.4%)."""
        snap = {
            "category": "crypto",
            "end_date": "2026-03-27T00:00:00Z",
            "created_at": "2026-03-25T12:00:00Z",
            "sm_registry_raw": 0.5,
            "whale_contribution_raw": 0.05,
            "whale_tx_count": 1,
        }
        result = classify_snapshot(snap)
        assert result.tier == Tier.T1
        assert result.expected_accuracy >= 0.96

    def test_tier1_politics_contra_long(self):
        """Politics >7d + contra + reg- = Tier 1 (100%)."""
        snap = {
            "category": "politics",
            "end_date": "2026-04-05T00:00:00Z",
            "created_at": "2026-03-25T12:00:00Z",
            "sm_registry_raw": 0,
            "whale_contribution_raw": -0.15,
            "whale_tx_count": 3,
        }
        result = classify_snapshot(snap)
        assert result.tier == Tier.T1
        assert result.expected_accuracy == 1.0

    def test_tier1_sports_bulk(self):
        """Sports >7d + no_wh + reg- = Tier 1B bulk (80.7%)."""
        snap = {
            "category": "sports",
            "end_date": "2026-04-05T00:00:00Z",
            "created_at": "2026-03-25T12:00:00Z",
            "sm_registry_raw": 0,
            "whale_contribution_raw": 0,
            "whale_tx_count": 0,
        }
        result = classify_snapshot(snap)
        assert result.tier == Tier.T1
        assert 0.80 <= result.expected_accuracy <= 0.82

    def test_tier2_crypto_no_whale(self):
        """Crypto <24h + no_wh + reg- = Tier 2 (70.5%)."""
        snap = {
            "category": "crypto",
            "end_date": "2026-03-26T00:00:00Z",
            "created_at": "2026-03-25T12:00:00Z",
            "sm_registry_raw": 0,
            "whale_contribution_raw": 0,
            "whale_tx_count": 0,
        }
        result = classify_snapshot(snap)
        assert result.tier == Tier.T2
        assert 0.70 <= result.expected_accuracy <= 0.72

    def test_tier3_culture_no_wh(self):
        """Culture <24h + no_wh + reg- = Tier 3 (49.0%) — was Tier 1 in v1!"""
        snap = {
            "category": "culture",
            "end_date": "2026-03-26T00:00:00Z",
            "created_at": "2026-03-25T12:00:00Z",
            "sm_registry_raw": 0,
            "whale_contribution_raw": 0,
            "whale_tx_count": 0,
        }
        result = classify_snapshot(snap)
        assert result.tier == Tier.T3
        assert result.expected_accuracy < 0.50

    def test_tier3_avoid_sports_weak_reg_plus(self):
        """Sports 1-3d + weak + reg+ = Tier 3 (46.1%)."""
        snap = {
            "category": "sports",
            "end_date": "2026-03-27T00:00:00Z",
            "created_at": "2026-03-25T12:00:00Z",
            "sm_registry_raw": 0.3,
            "whale_contribution_raw": 0.05,
            "whale_tx_count": 1,
        }
        result = classify_snapshot(snap)
        assert result.tier == Tier.T3

    def test_snapshot_created_at_alias(self):
        """compute_live_signals использует snapshot_created_at — должен работать."""
        snap = {
            "category": "politics",
            "end_date": "2026-04-05T00:00:00Z",
            "snapshot_created_at": "2026-03-25T12:00:00Z",
            "sm_registry_raw": 0,
            "whale_contribution_raw": -0.15,
            "whale_tx_count": 3,
        }
        result = classify_snapshot(snap)
        assert result.tier == Tier.T1

    def test_no_end_date_fallback(self):
        """Без end_date → segment unknown → fallback."""
        snap = {
            "category": "crypto",
            "end_date": None,
            "created_at": "2026-03-25T12:00:00Z",
            "sm_registry_raw": 0,
            "whale_contribution_raw": 0,
            "whale_tx_count": 0,
        }
        result = classify_snapshot(snap)
        assert result.tier in (Tier.T2, Tier.T3)

    def test_negative_hours_returns_unknown(self):
        """Negative hours_to_resolve → unknown segment (bug fix v3)."""
        snap = {
            "category": "sports",
            "end_date": "2026-03-20T00:00:00Z",
            "created_at": "2026-03-25T12:00:00Z",
            "sm_registry_raw": 0,
            "whale_contribution_raw": 0,
            "whale_tx_count": 0,
        }
        result = classify_snapshot(snap)
        # Should NOT match any table entry — segment = unknown → fallback
        assert "unknown" in result.cluster_label

    def test_fade_signal_in_table(self):
        """Tier 3 agree кластер = fade signal с инвертированной accuracy."""
        snap = {
            "category": "culture",
            "end_date": "2026-03-30T00:00:00Z",
            "created_at": "2026-03-25T12:00:00Z",
            "sm_registry_raw": 0,
            "whale_contribution_raw": 0.3,
            "whale_tx_count": 1,
        }
        result = classify_snapshot(snap)
        assert result.tier == Tier.T3
        assert result.is_fade_signal is True
        # accuracy inverted: 1 - 0.367 = 0.633
        assert result.expected_accuracy > 0.60

    def test_fallback_weak_reg_plus(self):
        """Fallback: weak + reg+ паттерн → Tier 2 с бонусом."""
        snap = {
            "category": "world & economy",
            "end_date": "2026-03-27T00:00:00Z",
            "created_at": "2026-03-25T12:00:00Z",
            "sm_registry_raw": 0.5,
            "whale_contribution_raw": 0.03,
            "whale_tx_count": 1,
        }
        result = classify_snapshot(snap)
        assert result.tier == Tier.T2
        assert "fallback" in result.cluster_label

    def test_fallback_contra_long_resolve(self):
        """Fallback: contra + >7d → Tier 2 с бонусом."""
        snap = {
            "category": "world & economy",
            "end_date": "2026-04-05T00:00:00Z",
            "created_at": "2026-03-25T12:00:00Z",
            "sm_registry_raw": 0,
            "whale_contribution_raw": -0.15,
            "whale_tx_count": 2,
        }
        result = classify_snapshot(snap)
        assert result.tier == Tier.T2
        assert "fallback" in result.cluster_label

    def test_unknown_category_fallback(self):
        """Неизвестная категория → дефолтный tier."""
        snap = {
            "category": "something_new",
            "end_date": "2026-03-28T00:00:00Z",
            "created_at": "2026-03-25T12:00:00Z",
            "sm_registry_raw": 0,
            "whale_contribution_raw": 0,
            "whale_tx_count": 0,
        }
        result = classify_snapshot(snap)
        assert result.tier in (Tier.T2, Tier.T3)
