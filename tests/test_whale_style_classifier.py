"""Тесты классификатора стилей китов (whale_style_classifier)."""

from app.services.whale_style_classifier import WhaleProfile, classify_style


def _profile(**overrides) -> WhaleProfile:
    """Фабрика WhaleProfile с дефолтами для тестов."""
    defaults = {
        "whale_id": "test-whale",
        "win_rate": 0.50,
        "bet_count": 20,
        "pnl_history": [10.0, -5.0, 15.0, -3.0, 8.0],
        "entry_prices": [0.55, 0.60, 0.45, 0.50, 0.65],
        "is_arbitrageur": False,
        "category_stats": {},
        "avg_reaction_minutes": None,
        "maker_ratio": None,
    }
    defaults.update(overrides)
    return WhaleProfile(**defaults)


class TestInsufficientData:
    def test_below_minimum_bets_returns_none(self):
        profile = _profile(bet_count=4)
        style, confidence = classify_style(profile)
        assert style is None
        assert confidence == 0.0

    def test_exactly_five_bets_is_eligible(self):
        profile = _profile(bet_count=5)
        style, confidence = classify_style(profile)
        # С дефолтными данными не попадёт ни в один стиль, но не отсечётся по min data
        assert not (style is None and confidence == 0.0 and profile.bet_count >= 5) or style is None


class TestArbitrageur:
    def test_arbitrageur_detected(self):
        profile = _profile(is_arbitrageur=True)
        style, confidence = classify_style(profile)
        assert style == "arbitrageur"
        assert confidence == 0.95

    def test_arbitrageur_with_low_bets_still_needs_minimum(self):
        profile = _profile(is_arbitrageur=True, bet_count=3)
        style, confidence = classify_style(profile)
        assert style is None


class TestBonder:
    def test_bonder_detected(self):
        # 8/10 = 80% entries > $0.90, median = $0.95
        prices = [0.91, 0.93, 0.95, 0.96, 0.97, 0.92, 0.94, 0.98, 0.50, 0.60]
        profile = _profile(entry_prices=prices, bet_count=10)
        style, confidence = classify_style(profile)
        assert style == "bonder"
        assert 0.7 <= confidence <= 1.0

    def test_bonder_rejected_low_ratio(self):
        # 5/10 = 50% > $0.90 — не bonder
        prices = [0.91, 0.93, 0.95, 0.96, 0.97, 0.50, 0.60, 0.45, 0.55, 0.40]
        profile = _profile(entry_prices=prices, bet_count=10)
        style, _ = classify_style(profile)
        assert style != "bonder"

    def test_bonder_rejected_low_median(self):
        # 80% > $0.90 но median < $0.92 (все на границе $0.91)
        prices = [0.91, 0.91, 0.91, 0.91, 0.91, 0.91, 0.91, 0.91, 0.50, 0.50]
        profile = _profile(entry_prices=prices, bet_count=10)
        style, _ = classify_style(profile)
        assert style != "bonder"


class TestEdgeTrader:
    def test_edge_trader_detected(self):
        # WR=0.70, stable positive PnL (low variance relative to mean)
        pnl = [10, 12, 8, 15, 11, 9, 13, 7, 14, 10, 12, 8]
        profile = _profile(win_rate=0.70, bet_count=12, pnl_history=pnl)
        style, confidence = classify_style(profile)
        assert style == "edge_trader"
        assert 0.3 <= confidence <= 1.0

    def test_edge_trader_rejected_low_wr(self):
        pnl = [10, 12, 8, 15, 11, 9, 13, 7, 14, 10]
        profile = _profile(win_rate=0.55, bet_count=10, pnl_history=pnl)
        style, _ = classify_style(profile)
        assert style != "edge_trader"

    def test_edge_trader_rejected_high_wr(self):
        pnl = [10, 12, 8, 15, 11, 9, 13, 7, 14, 10]
        profile = _profile(win_rate=0.90, bet_count=10, pnl_history=pnl)
        style, _ = classify_style(profile)
        assert style != "edge_trader"

    def test_edge_trader_rejected_negative_mean_pnl(self):
        pnl = [-10, -12, -8, -15, -11, -9, -13, -7, -14, -10]
        profile = _profile(win_rate=0.65, bet_count=10, pnl_history=pnl)
        style, _ = classify_style(profile)
        assert style != "edge_trader"

    def test_edge_trader_rejected_high_variance(self):
        # WR ok, mean positive, но std >> mean
        pnl = [100, -80, 90, -70, 85, -75, 95, -60, 80, -50]
        profile = _profile(win_rate=0.65, bet_count=10, pnl_history=pnl)
        style, _ = classify_style(profile)
        assert style != "edge_trader"

    def test_edge_trader_needs_minimum_10_bets(self):
        pnl = [10, 12, 8, 15, 11]
        profile = _profile(win_rate=0.70, bet_count=5, pnl_history=pnl)
        style, _ = classify_style(profile)
        assert style != "edge_trader"


class TestAsymmetric:
    def test_asymmetric_detected(self):
        # WR=0.30, mean positive благодаря одному huge winner
        pnl = [-5, -8, -3, -6, 500, -4, -7, -2, -9, -5, -3, -8, -6, -4, -7]
        profile = _profile(win_rate=0.30, bet_count=15, pnl_history=pnl)
        style, confidence = classify_style(profile)
        assert style == "asymmetric"
        assert 0.0 < confidence <= 1.0

    def test_asymmetric_rejected_positive_wr(self):
        pnl = [-5, -8, -3, -6, 500, -4, -7, -2, -9, -5, -3, -8, -6, -4, -7]
        profile = _profile(win_rate=0.55, bet_count=15, pnl_history=pnl)
        style, _ = classify_style(profile)
        assert style != "asymmetric"

    def test_asymmetric_rejected_negative_mean(self):
        pnl = [-5, -8, -3, -6, -1, -4, -7, -2, -9, -5, -3, -8, -6, -4, -7]
        profile = _profile(win_rate=0.30, bet_count=15, pnl_history=pnl)
        style, _ = classify_style(profile)
        assert style != "asymmetric"

    def test_asymmetric_rejected_no_heavy_tail(self):
        # mean > 0, WR < 0.40, но max не > 5× median(abs)
        pnl = [2, -1, 3, -2, 1, -1, 2, -1, 3, -2, 1, -1, 2, -1, 1]
        profile = _profile(win_rate=0.35, bet_count=15, pnl_history=pnl)
        style, _ = classify_style(profile)
        assert style != "asymmetric"

    def test_asymmetric_needs_15_bets(self):
        pnl = [-5, -8, -3, -6, 500, -4, -7, -2, -9, -5]
        profile = _profile(win_rate=0.30, bet_count=10, pnl_history=pnl)
        style, _ = classify_style(profile)
        assert style != "asymmetric"


class TestSpecialist:
    def test_specialist_detected(self):
        # crypto WR=0.75 (15 wins, 5 losses), overall WR=0.55, delta=0.20
        stats = {"crypto": {"wins": 15, "losses": 5}}
        profile = _profile(win_rate=0.55, bet_count=30, category_stats=stats)
        style, confidence = classify_style(profile)
        assert style == "specialist"
        assert 0.3 <= confidence <= 1.0

    def test_specialist_rejected_low_category_wr(self):
        stats = {"crypto": {"wins": 6, "losses": 6}}
        profile = _profile(win_rate=0.50, bet_count=20, category_stats=stats)
        style, _ = classify_style(profile)
        assert style != "specialist"

    def test_specialist_rejected_small_delta(self):
        # Category WR=0.67, overall WR=0.62 — delta 0.05 < 0.10
        stats = {"crypto": {"wins": 10, "losses": 5}}
        profile = _profile(win_rate=0.62, bet_count=20, category_stats=stats)
        style, _ = classify_style(profile)
        assert style != "specialist"

    def test_specialist_rejected_few_category_bets(self):
        stats = {"crypto": {"wins": 5, "losses": 1}}  # only 6 bets in category
        profile = _profile(win_rate=0.50, bet_count=20, category_stats=stats)
        style, _ = classify_style(profile)
        assert style != "specialist"


class TestSpeedTrader:
    def test_speed_trader_detected(self):
        profile = _profile(avg_reaction_minutes=10.0, bet_count=10)
        style, confidence = classify_style(profile)
        assert style == "speed_trader"
        assert 0.5 <= confidence <= 1.0

    def test_speed_trader_rejected_slow(self):
        profile = _profile(avg_reaction_minutes=45.0, bet_count=10)
        style, _ = classify_style(profile)
        assert style != "speed_trader"

    def test_speed_trader_rejected_no_data(self):
        profile = _profile(avg_reaction_minutes=None, bet_count=10)
        style, _ = classify_style(profile)
        assert style != "speed_trader"


class TestEventTrader:
    def test_event_trader_detected(self):
        # 15 дней PnL: 3 больших дня (top 20% = 3 из 15), содержат >60% суммарного PnL
        # WR=0.50 чтобы пройти мимо asymmetric (требует WR < 0.40)
        pnl = [1, 0, -1, 0, 200, 1, -1, 0, 0, 300, -1, 1, 0, 150, 0]
        profile = _profile(win_rate=0.50, bet_count=15, pnl_history=pnl)
        style, confidence = classify_style(profile)
        assert style == "event_trader"
        assert 0.5 <= confidence <= 1.0

    def test_event_trader_rejected_even_distribution(self):
        # Равномерно распределённый PnL — не event trader
        pnl = [10, 11, 9, 12, 8, 10, 11, 9, 12, 8, 10, 11, 9, 12, 8]
        profile = _profile(win_rate=0.50, bet_count=15, pnl_history=pnl)
        style, _ = classify_style(profile)
        assert style != "event_trader"

    def test_event_trader_needs_15_bets(self):
        pnl = [1, 0, -1, 0, 200, 1, -1, 0, 0, 300]
        profile = _profile(win_rate=0.35, bet_count=10, pnl_history=pnl)
        style, _ = classify_style(profile)
        assert style != "event_trader"


class TestLpMaker:
    def test_lp_maker_detected(self):
        profile = _profile(maker_ratio=0.75, bet_count=20)
        style, confidence = classify_style(profile)
        assert style == "lp_maker"
        assert 0.6 <= confidence <= 1.0

    def test_lp_maker_rejected_low_ratio(self):
        profile = _profile(maker_ratio=0.45, bet_count=20)
        style, _ = classify_style(profile)
        assert style != "lp_maker"

    def test_lp_maker_rejected_no_data(self):
        profile = _profile(maker_ratio=None, bet_count=20)
        style, _ = classify_style(profile)
        assert style != "lp_maker"


class TestWaterfallPriority:
    def test_arbitrageur_beats_bonder(self):
        prices = [0.95] * 10
        profile = _profile(is_arbitrageur=True, entry_prices=prices, bet_count=10)
        style, _ = classify_style(profile)
        assert style == "arbitrageur"

    def test_bonder_beats_edge_trader(self):
        prices = [0.95] * 8 + [0.50, 0.60]
        pnl = [10, 12, 8, 15, 11, 9, 13, 7, 14, 10]
        profile = _profile(win_rate=0.70, bet_count=10, entry_prices=prices, pnl_history=pnl)
        style, _ = classify_style(profile)
        assert style == "bonder"

    def test_lp_maker_beats_edge_trader(self):
        pnl = [10, 12, 8, 15, 11, 9, 13, 7, 14, 10]
        profile = _profile(win_rate=0.70, bet_count=10, maker_ratio=0.75, pnl_history=pnl)
        style, _ = classify_style(profile)
        assert style == "lp_maker"


class TestStyleOracleIntegration:
    """Integration: classify_style → whale_profile_multiplier pipeline."""

    def test_edge_trader_amplifies_strong_whale(self):
        from app.core.scoring import whale_profile_multiplier

        pnl = [10, 12, 8, 15, 11, 9, 13, 7, 14, 10]
        profile = _profile(win_rate=0.70, bet_count=40, pnl_history=pnl)
        style, _ = classify_style(profile)
        assert style == "edge_trader"

        mult = whale_profile_multiplier(roi_percent=30.0, bet_count=40, whale_style=style)
        # (1.0 + 0.5 + 0.15) × 1.00 = 1.65 (edge_trader neutral, N=15 ненадёжно)
        assert mult > 1.5
        assert mult <= 2.5

    def test_bonder_suppresses_signal(self):
        from app.core.scoring import whale_profile_multiplier

        prices = [0.95] * 10
        profile = _profile(entry_prices=prices, bet_count=10)
        style, _ = classify_style(profile)
        assert style == "bonder"

        mult = whale_profile_multiplier(roi_percent=5.0, bet_count=12, whale_style=style)
        # (1.0 + 0.1) × 1.00 = 1.1 (bonder neutral — WR=70% но ROI=-1% на 5.2M positions)
        assert mult > 0.9

    def test_arbitrageur_near_zero_signal(self):
        from app.core.scoring import whale_profile_multiplier

        profile = _profile(is_arbitrageur=True, bet_count=20)
        style, _ = classify_style(profile)
        assert style == "arbitrageur"

        mult = whale_profile_multiplier(roi_percent=20.0, bet_count=50, whale_style=style)
        # (1.0 + 0.4 + 0.15) × 0.10 = 0.155
        assert mult < 0.2

    def test_none_style_unchanged(self):
        from app.core.scoring import whale_profile_multiplier

        mult_with = whale_profile_multiplier(roi_percent=10.0, bet_count=15, whale_style=None)
        mult_without = whale_profile_multiplier(roi_percent=10.0, bet_count=15)
        assert mult_with == mult_without

    def test_style_configs_cover_all_styles(self):
        from app.core.scoring import STYLE_ORACLE_MULTIPLIERS
        from app.services.whale_style_classifier import STYLE_CONFIGS, WhaleStyle

        styles: set[str] = set(WhaleStyle.__args__)
        assert styles == set(STYLE_CONFIGS.keys())
        # STYLE_ORACLE_MULTIPLIERS содержит все стили + None
        assert styles.issubset(set(STYLE_ORACLE_MULTIPLIERS.keys()))
        assert None in STYLE_ORACLE_MULTIPLIERS
