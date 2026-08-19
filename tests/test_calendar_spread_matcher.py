"""Тесты для матчера календарных серий (D46 calendar-spread arb, research/fable-strategy-brief-2026-07-03/).

Regex-парсинг question/slug в (asset, direction, strike, end_date) — Gamma не отдаёт
готового group-поля для серий с разными датами экспирации одного порога (verified
2026-07-03, см. memory). Ложноположительная пара стоит денег на реальном арбитраже,
поэтому матчер тестируется отдельно от сетевого сборщика.
"""

from __future__ import annotations

from datetime import date

import pytest

from scripts.research.calendar_spread_matcher import (
    ParsedMarket,
    match_crypto_threshold,
    match_politics_series,
)


class TestCryptoThresholdMatcher:
    def test_matches_basic_btc_by_date(self):
        parsed = match_crypto_threshold(
            question="Will Bitcoin reach $150,000 by December 31, 2026?",
            slug="will-bitcoin-reach-150000-by-december-31-2026",
        )
        assert parsed == ParsedMarket(asset="BTC", strike=150_000.0, end_date=date(2026, 12, 31))

    def test_matches_hit_synonym_and_dollar_sign_variants(self):
        parsed = match_crypto_threshold(
            question="Will BTC hit $100k by July 31, 2026?",
            slug="btc-100k-jul-31",
        )
        assert parsed == ParsedMarket(asset="BTC", strike=100_000.0, end_date=date(2026, 7, 31))

    def test_matches_eth(self):
        parsed = match_crypto_threshold(
            question="Will Ethereum reach $10,000 by March 1, 2027?",
            slug="eth-10000-mar-1-2027",
        )
        assert parsed == ParsedMarket(asset="ETH", strike=10_000.0, end_date=date(2027, 3, 1))

    def test_rejects_non_threshold_question(self):
        assert (
            match_crypto_threshold(
                question="Will the Fed cut rates in September?",
                slug="fed-rate-cut-september",
            )
            is None
        )

    def test_rejects_range_question_not_single_threshold(self):
        # "between $X and $Y" — не единичный порог, другая структура рынка, не наша серия.
        assert (
            match_crypto_threshold(
                question="Will BTC be between $90,000 and $110,000 on July 31, 2026?",
                slug="btc-range-jul-31",
            )
            is None
        )

    def test_rejects_missing_date(self):
        assert (
            match_crypto_threshold(
                question="Will Bitcoin reach $150,000?",
                slug="will-bitcoin-reach-150000",
            )
            is None
        )

    @pytest.mark.parametrize("asset_alias", ["Bitcoin", "BTC", "bitcoin"])
    def test_asset_alias_normalizes_to_btc(self, asset_alias):
        parsed = match_crypto_threshold(
            question=f"Will {asset_alias} reach $50,000 by June 1, 2026?",
            slug="irrelevant-slug",
        )
        assert parsed is not None
        assert parsed.asset == "BTC"


class TestPoliticsSeriesMatcher:
    def test_matches_nomination_by_date(self):
        parsed = match_politics_series(
            question="Will Jane Doe be the Democratic nominee by August 1, 2028?",
            slug="jane-doe-dem-nominee-aug-2028",
        )
        assert parsed is not None
        assert parsed.entity == "Jane Doe"
        assert parsed.end_date == date(2028, 8, 1)

    def test_rejects_question_without_entity_and_date(self):
        assert (
            match_politics_series(
                question="Will there be a government shutdown?",
                slug="government-shutdown",
            )
            is None
        )

    def test_rejects_ambiguous_multi_entity_question(self):
        # Матчер должен явно НЕ угадывать при двух именах — false positive здесь дороже,
        # чем пропущенный сигнал (ставим реальные деньги на пару).
        assert (
            match_politics_series(
                question="Will Jane Doe or John Smith win the primary by August 1, 2028?",
                slug="primary-aug-2028",
            )
            is None
        )
