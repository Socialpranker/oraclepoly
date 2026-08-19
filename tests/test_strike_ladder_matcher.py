"""Тесты для матчера D49 strike-ladder (app/detectors/strike_ladder_matcher.py).

Провенанс: бэктест research/d49-strike-ladder-2026-07-09/answer.md нашёл три
критичных дефекта матчера — тесты покрывают все три регрессии (window_key
раздельность, touch-семантику exclude, dedup по strike) плюс базовый позитив.
"""

from __future__ import annotations

from datetime import date

from app.detectors.strike_ladder_matcher import (
    ParsedStrike,
    build_ladders,
    match_strike_ladder_market,
)


class TestMatchStrikeLadderMarket:
    def test_matches_basic_above_question(self):
        parsed = match_strike_ladder_market(
            question="Will the price of Bitcoin be above $68,000 on July 10?",
            end_date=date(2026, 7, 10),
            market_id="0xabc",
        )
        assert parsed == ParsedStrike(
            entity="BTC",
            strike=68_000.0,
            end_date=date(2026, 7, 10),
            market_id="0xabc",
            question="Will the price of Bitcoin be above $68,000 on July 10?",
            window_key="will the price of bitcoin be on july 10",
            token_id=None,
        )

    def test_matches_k_suffix_and_exceed_synonym(self):
        parsed = match_strike_ladder_market(
            question="Will ETH exceed $10k by December 31?",
            end_date=date(2026, 12, 31),
            market_id="0xdef",
        )
        assert parsed is not None
        assert parsed.entity == "ETH"
        assert parsed.strike == 10_000.0

    def test_excludes_reach_touch_semantics(self):
        """SOL 'reach $130 in November' — touch-anytime, не однонаправленный above.
        Verified 2026-07-09: reach/hit НЕ гарантируют монотонность (см. answer.md §3)."""
        assert (
            match_strike_ladder_market(
                question="Will Solana reach $130 in November?",
                end_date=date(2026, 11, 30),
                market_id="0x111",
            )
            is None
        )

    def test_excludes_below_and_between_and_dip_to(self):
        for q in (
            "Will BTC be below $50,000 on July 10?",
            "Will BTC be between $50,000 and $60,000 on July 10?",
            "Will BTC dip to $40,000 by July 10?",
        ):
            assert match_strike_ladder_market(question=q, end_date=date(2026, 7, 10), market_id="0x222") is None

    def test_rejects_unknown_asset(self):
        assert (
            match_strike_ladder_market(
                question="Will Cardano be above $2 on July 10?",
                end_date=date(2026, 7, 10),
                market_id="0x333",
            )
            is None
        )

    def test_rejects_missing_strike(self):
        assert (
            match_strike_ladder_market(
                question="Will Bitcoin hit a new all-time high?",
                end_date=date(2026, 7, 10),
                market_id="0x444",
            )
            is None
        )


class TestWindowKeySeparation:
    """Критичный дефект #2 (verified на BTC 'reach $116K August 25-31' won=0 vs
    'reach $120K in August' won=1 — тот же end_date, разные окна). Above-only
    вопросы должны так же разделяться по хвосту, если формулировка окна разная."""

    def test_same_end_date_different_window_text_not_merged(self):
        candidates = [
            {
                "market_id": "0x1",
                "question": "Will BTC be above $100,000 August 25-31?",
                "end_date": date(2026, 8, 31),
            },
            {
                "market_id": "0x2",
                "question": "Will BTC be above $110,000 in August?",
                "end_date": date(2026, 8, 31),
            },
        ]
        ladders = build_ladders(candidates)
        # Разные window_key → 2 отдельные однострайковые группы, ни одна не лесенка (>=2 rungs)
        assert ladders == {}

    def test_same_window_text_different_strikes_forms_ladder(self):
        candidates = [
            {"market_id": "0x1", "question": "Will BTC be above $58,000 on July 10?", "end_date": date(2026, 7, 10)},
            {"market_id": "0x2", "question": "Will BTC be above $60,000 on July 10?", "end_date": date(2026, 7, 10)},
            {"market_id": "0x3", "question": "Will BTC be above $62,000 on July 10?", "end_date": date(2026, 7, 10)},
        ]
        ladders = build_ladders(candidates)
        assert len(ladders) == 1
        (key, rungs), = ladders.items()
        assert key[0] == "BTC"
        assert key[1] == date(2026, 7, 10)
        assert [r.strike for r in rungs] == [58_000.0, 60_000.0, 62_000.0]
        assert [r.market_id for r in rungs] == ["0x1", "0x2", "0x3"]


class TestDedupByStrike:
    """Критичный дефект #3 (dedup): тот же (entity,end_date,window_key,strike) с
    РАЗНЫМИ market_id/question — двусмысленно, пропускаем весь strike."""

    def test_ambiguous_strike_slot_dropped_entirely(self):
        candidates = [
            {"market_id": "0x1", "question": "Will BTC be above $60,000 on July 10?", "end_date": date(2026, 7, 10)},
            # Дубль того же strike с другим market_id и слегка другим текстом вопроса
            # (тот же window_key после strip — оба "on july 10")
            {"market_id": "0x9", "question": "Will BTC be above $60,000 on July 10?", "end_date": date(2026, 7, 10)},
            {"market_id": "0x2", "question": "Will BTC be above $62,000 on July 10?", "end_date": date(2026, 7, 10)},
        ]
        ladders = build_ladders(candidates)
        # $60,000 слот двусмысленный (2 разных market_id) → дропнут, остаётся 1 rung (62000) < 2 → не лесенка
        assert ladders == {}

    def test_same_market_id_repeated_row_not_treated_as_ambiguous(self):
        candidates = [
            {"market_id": "0x1", "question": "Will BTC be above $60,000 on July 10?", "end_date": date(2026, 7, 10)},
            {"market_id": "0x1", "question": "Will BTC be above $60,000 on July 10?", "end_date": date(2026, 7, 10)},
            {"market_id": "0x2", "question": "Will BTC be above $62,000 on July 10?", "end_date": date(2026, 7, 10)},
        ]
        ladders = build_ladders(candidates)
        assert len(ladders) == 1
        (_, rungs), = ladders.items()
        assert [r.strike for r in rungs] == [60_000.0, 62_000.0]


class TestBuildLaddersMisc:
    def test_single_strike_not_a_ladder(self):
        candidates = [
            {"market_id": "0x1", "question": "Will BTC be above $60,000 on July 10?", "end_date": date(2026, 7, 10)},
        ]
        assert build_ladders(candidates) == {}

    def test_none_end_date_skipped(self):
        candidates = [
            {"market_id": "0x1", "question": "Will BTC be above $60,000 on July 10?", "end_date": None},
            {"market_id": "0x2", "question": "Will BTC be above $62,000 on July 10?", "end_date": date(2026, 7, 10)},
        ]
        assert build_ladders(candidates) == {}

    def test_different_entities_not_merged(self):
        candidates = [
            {"market_id": "0x1", "question": "Will BTC be above $60,000 on July 10?", "end_date": date(2026, 7, 10)},
            {"market_id": "0x2", "question": "Will ETH be above $2,000 on July 10?", "end_date": date(2026, 7, 10)},
        ]
        assert build_ladders(candidates) == {}
