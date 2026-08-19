"""Тесты для whale_segment_classifier — чистая классификация без I/O."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services.whale_segment_classifier import (
    CATEGORY_MIN_BETS,
    CATEGORY_MIN_WR,
    CLUSTER_MIN_SCORE,
    FRESH_BIG_MAX_BET_COUNT,
    FRESH_BIG_MIN_AMOUNT,
    FRESH_MAX_AGE_DAYS,
    PROVEN_MIN_BET_COUNT,
    PROVEN_MIN_ROI,
    PROVEN_MIN_WR,
    SEGMENT_CONFIGS,
    SWARM_MAX_AVG_AMOUNT,
    SWARM_MIN_TX_COUNT,
    assign_segment,
    classify_batch,
    classify_whale,
)

NOW = datetime(2026, 3, 14, 12, 0, 0, tzinfo=UTC)


def _whale(
    *,
    whale_id: str = "w1",
    created_at: datetime | None = None,
    bet_count: int = 0,
    is_smart_money: bool = False,
    coordination_score: float = 0.0,
    roi: float = 0.0,
    win_rate: float = 0.0,
) -> dict:
    """Хелпер для создания whale_data."""
    if created_at is None:
        created_at = NOW - timedelta(days=30)
    return {
        "whale_id": whale_id,
        "created_at": created_at,
        "bet_count": bet_count,
        "is_smart_money": is_smart_money,
        "coordination_score": coordination_score,
        "roi": roi,
        "win_rate": win_rate,
    }


def _tx(
    *,
    whale_id: str = "w1",
    market_id: str = "m1",
    outcome: str = "yes",
    amount: float = 10_000.0,
    category: str = "Politics",
) -> dict:
    """Хелпер для создания транзакции."""
    return {
        "whale_id": whale_id,
        "market_id": market_id,
        "outcome": outcome,
        "amount": amount,
        "category": category,
    }


# ─── Level 1: classify_whale ────────────────────────────────────────────


class TestClassifyWhale:
    """Level 1 классификация кошелька."""

    def test_cluster_highest_priority(self):
        """coordination_score > 0.5 → cluster, даже если wallet свежий."""
        whale = _whale(
            coordination_score=0.6,
            created_at=NOW - timedelta(days=3),  # свежий
            bet_count=2,
        )
        assert classify_whale(whale, now=NOW) == "cluster"

    def test_smart_money_over_fresh(self):
        """Известный smart money с новым адресом → smart_money, не fresh."""
        whale = _whale(
            is_smart_money=True,
            created_at=NOW - timedelta(days=5),  # свежий
            bet_count=3,
        )
        assert classify_whale(whale, now=NOW) == "smart_money"

    def test_fresh_wallet(self):
        """Новый кошелёк (< 14 дней) → fresh."""
        whale = _whale(
            created_at=NOW - timedelta(days=10),
            bet_count=2,
        )
        assert classify_whale(whale, now=NOW) == "fresh"

    def test_fresh_wallet_boundary_13_days(self):
        """13 дней = всё ещё fresh."""
        whale = _whale(created_at=NOW - timedelta(days=13))
        assert classify_whale(whale, now=NOW) == "fresh"

    def test_experienced_wallet(self):
        """Старый кошелёк с >= 10 ставками → experienced."""
        whale = _whale(
            created_at=NOW - timedelta(days=60),
            bet_count=15,
            roi=5.0,
            win_rate=0.6,
        )
        assert classify_whale(whale, now=NOW) == "experienced"

    def test_experienced_boundary_14_days_10_bets(self):
        """Ровно 14 дней и 10 ставок → experienced."""
        whale = _whale(
            created_at=NOW - timedelta(days=14),
            bet_count=10,
        )
        assert classify_whale(whale, now=NOW) == "experienced"

    def test_unqualified_old_few_bets(self):
        """Старый кошелёк, мало ставок → None."""
        whale = _whale(
            created_at=NOW - timedelta(days=60),
            bet_count=5,
        )
        assert classify_whale(whale, now=NOW) is None

    def test_unqualified_no_data(self):
        """Старый кошелёк без ставок → None."""
        whale = _whale(
            created_at=NOW - timedelta(days=100),
            bet_count=0,
        )
        assert classify_whale(whale, now=NOW) is None

    def test_cluster_overrides_smart_money(self):
        """Cluster приоритет выше smart_money."""
        whale = _whale(
            coordination_score=0.7,
            is_smart_money=True,
        )
        assert classify_whale(whale, now=NOW) == "cluster"

    def test_coordination_score_boundary_exact_0_5(self):
        """coordination_score ровно 0.5 — НЕ cluster (нужно > 0.5)."""
        whale = _whale(coordination_score=0.5)
        # 0.5 не > 0.5, должно пойти по другим правилам
        assert classify_whale(whale, now=NOW) != "cluster"


# ─── Level 2: assign_segment ────────────────────────────────────────────


class TestAssignSegment:
    """Level 2 классификация транзакции в конкретный сегмент."""

    def test_cluster_all_txs_pass(self):
        """cluster → все транзакции проходят как 'cluster'."""
        tx = _tx(amount=100)
        result = assign_segment("cluster", _whale(), tx, {}, {})
        assert result == "cluster"

    def test_smart_money_all_txs_pass(self):
        """smart_money → все транзакции проходят."""
        tx = _tx(amount=50)
        result = assign_segment("smart_money", _whale(is_smart_money=True), tx, {}, {})
        assert result == "smart_money"

    # ── fresh ──

    def test_fresh_big(self):
        """fresh + amount >= $5K + bet_count < 5 → fresh_big."""
        whale = _whale(bet_count=3, created_at=NOW - timedelta(days=5))
        tx = _tx(amount=5_000.0)
        result = assign_segment("fresh", whale, tx, {}, {})
        assert result == "fresh_big"

    def test_fresh_big_large_amount(self):
        """fresh + amount $50K + bet_count=1 → fresh_big."""
        whale = _whale(bet_count=1, created_at=NOW - timedelta(days=2))
        tx = _tx(amount=50_000.0)
        result = assign_segment("fresh", whale, tx, {}, {})
        assert result == "fresh_big"

    def test_fresh_big_rejected_high_bet_count(self):
        """fresh + amount >= $5K, но bet_count >= 5 → не fresh_big."""
        whale = _whale(bet_count=5, created_at=NOW - timedelta(days=5))
        tx = _tx(amount=10_000.0)
        # Не fresh_big, проверяем swarm
        result = assign_segment("fresh", whale, tx, {}, {})
        # bet_count >= 5, amount >= $5K → не fresh_big, нет swarm → None
        assert result is None

    def test_fresh_swarm(self):
        """fresh + tx в swarm группе (count >= 3, avg < $1K) → fresh_swarm."""
        whale = _whale(bet_count=2, created_at=NOW - timedelta(days=3))
        tx = _tx(whale_id="w1", market_id="m1", outcome="yes", amount=500.0)
        swarm_groups = {
            ("w1", "m1", "yes"): {"count": 4, "avg_amount": 800.0},
        }
        result = assign_segment("fresh", whale, tx, swarm_groups, {})
        assert result == "fresh_swarm"

    def test_fresh_swarm_boundary(self):
        """Swarm ровно на границе: count=3, avg=999.99."""
        whale = _whale(bet_count=2, created_at=NOW - timedelta(days=3))
        tx = _tx(whale_id="w1", market_id="m2", outcome="no", amount=500.0)
        swarm_groups = {
            ("w1", "m2", "no"): {"count": 3, "avg_amount": 999.99},
        }
        result = assign_segment("fresh", whale, tx, swarm_groups, {})
        assert result == "fresh_swarm"

    def test_fresh_swarm_rejected_low_count(self):
        """Swarm группа с count < 3 → не swarm."""
        whale = _whale(bet_count=2, created_at=NOW - timedelta(days=3))
        tx = _tx(whale_id="w1", market_id="m1", outcome="yes", amount=500.0)
        swarm_groups = {
            ("w1", "m1", "yes"): {"count": 2, "avg_amount": 500.0},
        }
        result = assign_segment("fresh", whale, tx, swarm_groups, {})
        assert result is None

    def test_fresh_swarm_rejected_high_avg(self):
        """Swarm группа с avg >= $1K → не swarm."""
        whale = _whale(bet_count=2, created_at=NOW - timedelta(days=3))
        tx = _tx(whale_id="w1", market_id="m1", outcome="yes", amount=1_500.0)
        swarm_groups = {
            ("w1", "m1", "yes"): {"count": 5, "avg_amount": 1_200.0},
        }
        result = assign_segment("fresh", whale, tx, swarm_groups, {})
        assert result is None

    def test_fresh_skip_no_pattern(self):
        """fresh, но нет паттерна (small bet, no swarm) → None."""
        whale = _whale(bet_count=3, created_at=NOW - timedelta(days=5))
        tx = _tx(amount=500.0)  # маленькая ставка
        result = assign_segment("fresh", whale, tx, {}, {})
        assert result is None

    def test_fresh_big_takes_priority_over_swarm(self):
        """fresh_big проверяется первым: если подходит — swarm не смотрим."""
        whale = _whale(bet_count=2, created_at=NOW - timedelta(days=3))
        tx = _tx(whale_id="w1", market_id="m1", outcome="yes", amount=5_000.0)
        swarm_groups = {
            ("w1", "m1", "yes"): {"count": 5, "avg_amount": 500.0},
        }
        result = assign_segment("fresh", whale, tx, swarm_groups, {})
        assert result == "fresh_big"

    # ── experienced ──

    def test_category_spec(self):
        """experienced + категория совпадает + wr >= 65% + bets >= 10 → category_spec."""
        whale = _whale(
            bet_count=50,
            created_at=NOW - timedelta(days=90),
            roi=5.0,
            win_rate=0.55,
        )
        tx = _tx(category="Politics")
        cat_stats = {
            "w1": {
                "Politics": {"win_rate": 0.70, "bets": 20},
            },
        }
        result = assign_segment("experienced", whale, tx, {}, cat_stats)
        assert result == "category_spec"

    def test_category_spec_wrong_category(self):
        """experienced, но tx категория не совпадает с экспертизой → falls to proven check."""
        whale = _whale(
            bet_count=50,
            created_at=NOW - timedelta(days=90),
            roi=15.0,
            win_rate=0.60,
        )
        tx = _tx(category="Sports")
        cat_stats = {
            "w1": {
                "Politics": {"win_rate": 0.70, "bets": 20},
                # Sports нет в статистике
            },
        }
        result = assign_segment("experienced", whale, tx, {}, cat_stats)
        assert result == "proven"

    def test_category_spec_low_wr(self):
        """experienced + категория совпадает, но wr < 65% → не category_spec."""
        whale = _whale(
            bet_count=50,
            created_at=NOW - timedelta(days=90),
            roi=12.0,
            win_rate=0.58,
        )
        tx = _tx(category="Politics")
        cat_stats = {
            "w1": {
                "Politics": {"win_rate": 0.60, "bets": 15},  # wr < 0.65
            },
        }
        result = assign_segment("experienced", whale, tx, {}, cat_stats)
        # Не category_spec, но roi > 10 + wr > 0.55 → proven
        assert result == "proven"

    def test_category_spec_low_bets(self):
        """experienced + категория совпадает + wr >= 65%, но bets < 10 → не category_spec."""
        whale = _whale(
            bet_count=50,
            created_at=NOW - timedelta(days=90),
            roi=12.0,
            win_rate=0.58,
        )
        tx = _tx(category="Politics")
        cat_stats = {
            "w1": {
                "Politics": {"win_rate": 0.70, "bets": 8},  # bets < 10
            },
        }
        result = assign_segment("experienced", whale, tx, {}, cat_stats)
        assert result == "proven"

    def test_proven(self):
        """experienced + roi > 10 + wr > 55% → proven."""
        whale = _whale(
            bet_count=30,
            created_at=NOW - timedelta(days=60),
            roi=15.0,
            win_rate=0.60,
        )
        tx = _tx(category="Sports")
        result = assign_segment("experienced", whale, tx, {}, {})
        assert result == "proven"

    def test_proven_boundary(self):
        """roi ровно 10.0 — НЕ proven (нужно > 10). wr ровно 0.55 — НЕ proven (нужно > 0.55)."""
        whale = _whale(
            bet_count=30,
            created_at=NOW - timedelta(days=60),
            roi=10.0,
            win_rate=0.55,
        )
        tx = _tx()
        result = assign_segment("experienced", whale, tx, {}, {})
        assert result is None

    def test_proven_roi_boundary_passes(self):
        """roi=10.01, wr=0.56 → proven."""
        whale = _whale(
            bet_count=30,
            created_at=NOW - timedelta(days=60),
            roi=10.01,
            win_rate=0.56,
        )
        tx = _tx()
        result = assign_segment("experienced", whale, tx, {}, {})
        assert result == "proven"

    def test_experienced_unqualified(self):
        """experienced, но low roi + no category expertise → None."""
        whale = _whale(
            bet_count=20,
            created_at=NOW - timedelta(days=90),
            roi=3.0,
            win_rate=0.48,
        )
        tx = _tx(category="Crypto")
        result = assign_segment("experienced", whale, tx, {}, {})
        assert result is None

    def test_none_wallet_class_returns_none(self):
        """wallet_class=None → всегда None."""
        tx = _tx()
        result = assign_segment(None, _whale(), tx, {}, {})
        assert result is None

    # ── Комбинированный: fresh wallet с обоими паттернами ──

    def test_fresh_wallet_mixed_patterns(self):
        """Один whale, два market: big на market1, swarm на market2."""
        whale = _whale(bet_count=2, created_at=NOW - timedelta(days=5))

        tx_big = _tx(whale_id="w1", market_id="m1", amount=6_000.0)
        tx_swarm = _tx(whale_id="w1", market_id="m2", outcome="yes", amount=300.0)

        swarm_groups = {
            ("w1", "m2", "yes"): {"count": 5, "avg_amount": 400.0},
        }

        result_big = assign_segment("fresh", whale, tx_big, swarm_groups, {})
        result_swarm = assign_segment("fresh", whale, tx_swarm, swarm_groups, {})

        assert result_big == "fresh_big"
        assert result_swarm == "fresh_swarm"


# ─── classify_batch ──────────────────────────────────────────────────────


class TestClassifyBatch:
    """Группировка всех транзакций по сегментам."""

    def test_groups_correctly(self):
        """Три whale: cluster + fresh_big + proven → правильная группировка."""
        # Whale 1: cluster
        w1 = _whale(whale_id="w1", coordination_score=0.8)
        # Whale 2: fresh + big bet
        w2 = _whale(whale_id="w2", created_at=NOW - timedelta(days=3), bet_count=2)
        # Whale 3: experienced + proven
        w3 = _whale(
            whale_id="w3",
            created_at=NOW - timedelta(days=90),
            bet_count=30,
            roi=20.0,
            win_rate=0.65,
        )

        whale_txs = [
            _tx(whale_id="w1", market_id="m1", amount=1_000.0),
            _tx(whale_id="w1", market_id="m2", amount=500.0),
            _tx(whale_id="w2", market_id="m3", amount=8_000.0),
            _tx(whale_id="w3", market_id="m4", amount=15_000.0),
        ]

        whale_meta = {
            "w1": w1,
            "w2": w2,
            "w3": w3,
        }

        cluster_scores = {
            "w1": 0.8,
        }

        result = classify_batch(
            whale_txs=whale_txs,
            whale_meta=whale_meta,
            swarm_groups={},
            whale_category_stats={},
            cluster_scores=cluster_scores,
            now=NOW,
        )

        assert "cluster" in result
        assert len(result["cluster"]) == 2
        assert all(tx["whale_id"] == "w1" for tx in result["cluster"])

        assert "fresh_big" in result
        assert len(result["fresh_big"]) == 1
        assert result["fresh_big"][0]["whale_id"] == "w2"

        assert "proven" in result
        assert len(result["proven"]) == 1
        assert result["proven"][0]["whale_id"] == "w3"

    def test_empty_input(self):
        """Пустой вход → пустой dict."""
        result = classify_batch(
            whale_txs=[],
            whale_meta={},
            swarm_groups={},
            whale_category_stats={},
            cluster_scores={},
            now=NOW,
        )
        assert result == {}

    def test_skipped_txs_not_in_result(self):
        """Транзакции без сегмента не попадают в результат."""
        # Experienced, но low roi + low wr → unqualified
        w1 = _whale(
            whale_id="w1",
            created_at=NOW - timedelta(days=90),
            bet_count=20,
            roi=2.0,
            win_rate=0.45,
        )

        whale_txs = [_tx(whale_id="w1", market_id="m1")]
        whale_meta = {"w1": w1}

        result = classify_batch(
            whale_txs=whale_txs,
            whale_meta=whale_meta,
            swarm_groups={},
            whale_category_stats={},
            cluster_scores={},
            now=NOW,
        )

        assert result == {}

    def test_unknown_whale_id_skipped(self):
        """Транзакция с неизвестным whale_id (нет в whale_meta) → пропуск."""
        whale_txs = [_tx(whale_id="unknown", market_id="m1")]

        result = classify_batch(
            whale_txs=whale_txs,
            whale_meta={},
            swarm_groups={},
            whale_category_stats={},
            cluster_scores={},
            now=NOW,
        )

        assert result == {}

    def test_cluster_scores_override_whale_coordination(self):
        """cluster_scores извне перезаписывают coordination_score в whale_meta."""
        w1 = _whale(whale_id="w1", coordination_score=0.0)  # в meta — 0
        whale_txs = [_tx(whale_id="w1", market_id="m1")]
        whale_meta = {"w1": w1}

        result = classify_batch(
            whale_txs=whale_txs,
            whale_meta=whale_meta,
            swarm_groups={},
            whale_category_stats={},
            cluster_scores={"w1": 0.9},  # override
            now=NOW,
        )

        assert "cluster" in result
        assert len(result["cluster"]) == 1

    def test_mixed_segments_with_swarm(self):
        """Batch с swarm группой: fresh_swarm корректно определяется."""
        w1 = _whale(whale_id="w1", created_at=NOW - timedelta(days=5), bet_count=1)

        whale_txs = [
            _tx(whale_id="w1", market_id="m1", outcome="yes", amount=300.0),
            _tx(whale_id="w1", market_id="m1", outcome="yes", amount=200.0),
            _tx(whale_id="w1", market_id="m1", outcome="yes", amount=250.0),
        ]

        whale_meta = {"w1": w1}
        swarm_groups = {
            ("w1", "m1", "yes"): {"count": 3, "avg_amount": 250.0},
        }

        result = classify_batch(
            whale_txs=whale_txs,
            whale_meta=whale_meta,
            swarm_groups=swarm_groups,
            whale_category_stats={},
            cluster_scores={},
            now=NOW,
        )

        assert "fresh_swarm" in result
        assert len(result["fresh_swarm"]) == 3


# ─── SegmentConfig / SEGMENT_CONFIGS ────────────────────────────────────


class TestSegmentConfigs:
    """Проверка конфигов сегментов."""

    def test_all_segments_have_configs(self):
        """Все 6 сегментов имеют конфиг."""
        expected = {"cluster", "fresh_big", "fresh_swarm", "smart_money", "category_spec", "proven"}
        assert set(SEGMENT_CONFIGS.keys()) == expected

    def test_segment_config_frozen(self):
        """SegmentConfig неизменяемый."""
        cfg = SEGMENT_CONFIGS["cluster"]
        with pytest.raises(AttributeError):
            cfg.bank = 999  # type: ignore[misc]

    def test_cluster_config_values(self):
        """Конфиг cluster: bank=500, kelly_mult=1.0, max_bet_pct=0.10, drawdown_stop=0.15."""
        cfg = SEGMENT_CONFIGS["cluster"]
        assert cfg.bank == 500
        assert cfg.kelly_mult == 1.0
        assert cfg.max_bet_pct == 0.10
        assert cfg.drawdown_stop == 0.15

    def test_proven_config_values(self):
        """Конфиг proven: bank=1250, kelly_mult=0.25, max_bet_pct=0.03, drawdown_stop=0.25."""
        cfg = SEGMENT_CONFIGS["proven"]
        assert cfg.bank == 1250
        assert cfg.kelly_mult == 0.25
        assert cfg.max_bet_pct == 0.03
        assert cfg.drawdown_stop == 0.25


# ─── Константы ──────────────────────────────────────────────────────────


class TestConstants:
    """Проверка экспортируемых констант."""

    def test_fresh_max_age_days(self):
        assert FRESH_MAX_AGE_DAYS == 14

    def test_fresh_big_min_amount(self):
        assert FRESH_BIG_MIN_AMOUNT == 5_000.0

    def test_fresh_big_max_bet_count(self):
        assert FRESH_BIG_MAX_BET_COUNT == 5

    def test_swarm_min_tx_count(self):
        assert SWARM_MIN_TX_COUNT == 3

    def test_swarm_max_avg_amount(self):
        assert SWARM_MAX_AVG_AMOUNT == 1_000.0

    def test_category_min_bets(self):
        assert CATEGORY_MIN_BETS == 10

    def test_category_min_wr(self):
        assert CATEGORY_MIN_WR == 0.65

    def test_proven_min_roi(self):
        assert PROVEN_MIN_ROI == 10.0

    def test_proven_min_wr(self):
        assert PROVEN_MIN_WR == 0.55

    def test_proven_min_bet_count(self):
        assert PROVEN_MIN_BET_COUNT == 10

    def test_cluster_min_score(self):
        assert CLUSTER_MIN_SCORE == 0.5


# =====================================================================
# 8-уровневая система тиров (бывший test_whale_tiers.py)
# =====================================================================


class TestGetTransactionTier:
    """get_transaction_tier — тир сделки по сумме."""

    def test_shrimp_lower_bound(self):
        from app.core.whale_tiers import get_transaction_tier

        assert get_transaction_tier(5000) == "shrimp"

    def test_shrimp_upper_bound(self):
        from app.core.whale_tiers import get_transaction_tier

        assert get_transaction_tier(7499) == "shrimp"

    def test_crab(self):
        from app.core.whale_tiers import get_transaction_tier

        assert get_transaction_tier(7500) == "crab"

    def test_crab_upper(self):
        from app.core.whale_tiers import get_transaction_tier

        assert get_transaction_tier(9999) == "crab"

    def test_fish(self):
        from app.core.whale_tiers import get_transaction_tier

        assert get_transaction_tier(10000) == "fish"

    def test_fish_upper(self):
        from app.core.whale_tiers import get_transaction_tier

        assert get_transaction_tier(19999) == "fish"

    def test_dolphin(self):
        from app.core.whale_tiers import get_transaction_tier

        assert get_transaction_tier(20000) == "dolphin"

    def test_dolphin_upper(self):
        from app.core.whale_tiers import get_transaction_tier

        assert get_transaction_tier(49999) == "dolphin"

    def test_shark(self):
        from app.core.whale_tiers import get_transaction_tier

        assert get_transaction_tier(50000) == "shark"

    def test_whale(self):
        from app.core.whale_tiers import get_transaction_tier

        assert get_transaction_tier(100000) == "whale"

    def test_orca(self):
        from app.core.whale_tiers import get_transaction_tier

        assert get_transaction_tier(250000) == "orca"

    def test_leviathan(self):
        from app.core.whale_tiers import get_transaction_tier

        assert get_transaction_tier(1000000) == "leviathan"

    def test_leviathan_huge(self):
        from app.core.whale_tiers import get_transaction_tier

        assert get_transaction_tier(2_500_000) == "leviathan"

    def test_below_minimum_fallback(self):
        from app.core.whale_tiers import get_transaction_tier

        assert get_transaction_tier(1000) == "shrimp"

    def test_zero_fallback(self):
        from app.core.whale_tiers import get_transaction_tier

        assert get_transaction_tier(0) == "shrimp"

    def test_negative_fallback(self):
        from app.core.whale_tiers import get_transaction_tier

        assert get_transaction_tier(-100) == "shrimp"


class TestGetWhaleTier:
    """get_whale_tier — тир кита по avg_bet_size с low-data cap."""

    def test_normal_shark(self):
        from app.core.whale_tiers import get_whale_tier

        assert get_whale_tier(60000, bet_count=10) == "shark"

    def test_normal_leviathan(self):
        from app.core.whale_tiers import get_whale_tier

        assert get_whale_tier(1_500_000, bet_count=5) == "leviathan"

    def test_low_data_cap_high_avg(self):
        from app.core.whale_tiers import get_whale_tier

        assert get_whale_tier(200000, bet_count=1) == "fish"

    def test_low_data_cap_mid_avg(self):
        from app.core.whale_tiers import get_whale_tier

        assert get_whale_tier(8000, bet_count=2) == "crab"

    def test_low_data_no_cap_at_threshold(self):
        from app.core.whale_tiers import get_whale_tier

        assert get_whale_tier(200000, bet_count=3) == "whale"

    def test_none_avg_bet(self):
        from app.core.whale_tiers import get_whale_tier

        assert get_whale_tier(None, bet_count=0) == "shrimp"

    def test_zero_avg_bet(self):
        from app.core.whale_tiers import get_whale_tier

        assert get_whale_tier(0, bet_count=5) == "shrimp"

    def test_exact_boundary_fish(self):
        from app.core.whale_tiers import get_whale_tier

        assert get_whale_tier(10000, bet_count=10) == "fish"


class TestTierIndex:
    """tier_index — числовой индекс для сравнений."""

    def test_shrimp_is_0(self):
        from app.core.whale_tiers import tier_index

        assert tier_index("shrimp") == 0

    def test_leviathan_is_7(self):
        from app.core.whale_tiers import tier_index

        assert tier_index("leviathan") == 7

    def test_order_ascending(self):
        from app.core.whale_tiers import TIER_THRESHOLDS, tier_index

        tiers = [name for name, _, _ in TIER_THRESHOLDS]
        for i in range(len(tiers) - 1):
            assert tier_index(tiers[i]) < tier_index(tiers[i + 1])

    def test_unknown_tier_returns_minus_1(self):
        from app.core.whale_tiers import tier_index

        assert tier_index("unknown") == -1
