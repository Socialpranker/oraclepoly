"""
Bayesian Log-Odds Oracle Score Calculator.

Замена линейной модели S = alpha * P_sent + beta * P_whale - gamma * P_risk
на математически корректный Logarithmic Opinion Pool (Log-Linear Aggregation).

MATH_SPEC: §3.1-§3.5 — Core Framework
MATH_SPEC: §2.1-§2.4 — Calibration & Validation

Правило: чистая математика. Никаких DB, API, Redis, async.
Все данные приходят как аргументы функций.
"""

import asyncio
import math

# === Sub-step 3.1: Core Coordinate Transformations ===

EPSILON = 1e-5

# Shadow scoring task registry — prevents GC of background tasks (Phase 2).
_shadow_tasks: set[asyncio.Task] = set()

# === Open Interest ===
DEFAULT_MEDIAN_LIQUIDITY = 100_000  # Медианная ликвидность рынка Polymarket ($100K)
OI_SCALE = 0.3  # Масштаб OI Bayes Factor

# === Concentration ===
CONCENTRATION_BASELINE = 0.3  # Базовый уровень концентрации top5
CONCENTRATION_SCALE = 0.5  # Масштаб Concentration Factor
MIN_HOLDERS_FOR_SIGNAL = 5  # Минимум холдеров для сигнала

# === Divergence Clamp ===
# Анализ 414K resolved snapshots: oracle ВЫШЕ рынка → Brier +0.024 (убыточно),
# oracle НИЖЕ → Brier -0.001 (полезно). Asymmetric damping сохраняет downside edge.
# Подтверждено 2026-04-08 на 615K markets: YES zones 50-99% overpriced (bias +0.05..+0.68),
# longshots 01-30% underpriced. Optimism tax = systematic YES overpricing.
UPWARD_DAMPING = 0.3  # Dampen upward divergence (oracle > market) на 70%
MAX_DIVERGENCE = 0.10  # Абсолютный cap divergence от market_price


def safe_logit(p: float) -> float:
    """
    Перевод вероятности в log-odds пространство.

    §3.1 MATH_SPEC: p_safe = max(eps, min(1-eps, p)), Z = ln(p_safe/(1-p_safe))

    Args:
        p: Вероятность [0, 1]

    Returns:
        Log-odds Z in (-inf, +inf). При p=0.5 -> Z=0. При p->1 -> Z->+inf. При p->0 -> Z->-inf.
    """
    p_safe = max(EPSILON, min(1.0 - EPSILON, p))
    return math.log(p_safe / (1.0 - p_safe))


def stable_sigmoid(z: float) -> float:
    """
    Обратный перевод log-odds в вероятность. Piecewise для overflow-иммунитета.

    §3.1 MATH_SPEC:
        sigma(Z) = 1/(1+e^(-Z)) если Z >= 0
        sigma(Z) = e^Z/(1+e^Z) если Z < 0

    Гарантии: никогда не вернёт NaN, inf, или значение вне (0, 1).

    Args:
        z: Log-odds значение

    Returns:
        Вероятность in (0, 1)
    """
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    else:
        ez = math.exp(z)
        return ez / (1.0 + ez)


STYLE_ORACLE_MULTIPLIERS: dict[str | None, float] = {
    # Recalibrated 2026-04-08 on 6M+ resolved positions (418M trades dataset)
    # ROI-based ranking (WR unreliable due to Polymarket YES-bias):
    "speed_trader": 1.50,  # ROI +1.76%, WR=63.3%, N=13595 — единственный стиль с positive ROI
    "bonder": 1.00,  # ROI -1.07%, WR=70.0%, N=5.2M — neutral, высокий WR но negative ROI
    "event_trader": 1.00,  # Не выделяется из general на полных данных → neutral
    "edge_trader": 1.00,  # N=15 в prod, нет в историческом — neutral
    "lp_maker": 0.80,  # ROI -0.03%, WR=65.4%, N=557K — breakeven, понижен
    "specialist": 0.78,  # Prod-only: WR=44.4%, N=5767 — слабый
    "asymmetric": 0.50,  # Prod-only: WR=37.6%, N=2836 — антисигнал
    "arbitrageur": 0.10,  # Noise, не предсказывает direction
    None: 1.00,
}


def whale_profile_multiplier(
    roi_percent: float,
    bet_count: int,
    sector_win_rates: dict | None = None,
    market_category: str | None = None,
    whale_style: str | None = None,
    wins: int = 0,
    losses: int = 0,
) -> float:
    """
    Per-whale profile multiplier на основе трек-рекорда.

    Линейная формула, относительные метрики.
    Baseline = 1.0, диапазон [0.05, 2.5].

    Компоненты:
        ROI: 0.02 за каждый 1% ROI, clamp ±0.5
            roi=+25% → +0.5, roi=-25% → -0.5
        Experience: track_record >= 20 → +0.15, < 3 → -0.2
            track_record = max(bet_count, wins + losses)
        Sector: category-aware boost если >=10 сделок в категории
            Применяется ТОЛЬКО при roi_percent > 0 (gate)
            win_rate < 40% penalty применяется всегда
        Style: multiplicative modifier из STYLE_ORACLE_MULTIPLIERS
            Data-driven: bonder ×1.85, event_trader ×1.41, specialist ×0.78
            (blended WR по resolved txns 2026-02..04, N>2800)
    """
    m = 1.0

    # ROI: primary quality signal (relative performance)
    m += max(-0.5, min(0.5, roi_percent * 0.02))

    # Experience: confidence in ROI estimate (fix: use track_record)
    track_record = max(bet_count, wins + losses)
    if track_record >= 20:
        m += 0.15
    elif track_record < 3:
        m -= 0.2

    # Sector: category-specific boost, gated by ROI sign
    if isinstance(sector_win_rates, dict) and market_category:
        sector_data = sector_win_rates.get(market_category)
        if sector_data:
            s_wins = int(sector_data.get("wins", 0))
            s_losses = int(sector_data.get("losses", 0))
            total = s_wins + s_losses
            if total >= 10:
                win_rate = s_wins / total
                if roi_percent > 0:
                    # Прибыльный кит: полный sector boost
                    m += max(-0.25, min(0.25, (win_rate - 0.5) * 1.25))
                elif win_rate < 0.4:
                    # Убыточный кит: только penalty за низкий WR
                    m += max(-0.25, (win_rate - 0.5) * 1.25)

    # Style: multiplicative modifier (edge_trader усиливает, bonder гасит)
    style_mult = STYLE_ORACLE_MULTIPLIERS.get(whale_style, 1.0)

    return max(0.05, min(2.5, m * style_mult))


class BayesianScoreCalculator:
    """
    Bayesian Log-Odds Score Calculator.

    Заменяет линейную модель S = alpha * P_sent + beta * P_whale - gamma * P_risk
    на математически корректный Logarithmic Opinion Pool.

    Все вычисления на нативном float. Никаких Decimal, DB, API.
    """

    # Default константы (переопределяются через Settings)
    F_MAX = 0.25  # Quarter-Kelly cap
    K_LIQUIDITY = 0.01  # Liquidity gating tuning parameter

    # Default веса — синхронизированы с прод calibration_configs global (2026-04-05).
    # Это fallback: CalibrationService загружает per-category конфиги из БД → Redis.
    # ⚠️ НЕ менять без синхронизации с calibration_service.py _FALLBACK_CALIBRATION.
    DEFAULT_WEIGHTS: dict[str, float] = {
        "w_mkt": 0.6165,  # Прод global: market prior (beta calibration)
        "w_whale": 0.5,  # Прод: линейный whale активен
        "w_whale_contra": 0.0,  # Прод: fade-the-whale отключён
        "w_fresh": 0.0,  # Нет данных
        "w_cluster": 0.0,  # Нет данных
        "w_micro": 0.0,  # micro_factor удалён (2026-04-10), всегда 0.0
        "w_registry": 0.1,  # Прод global: SM registry
        "w_oi": 0.2,  # Прод: open interest
        "w_concentration": 0.0,  # Ухудшает accuracy
        "w_sports_odds": 0.0,  # Данные начали записываться 2026-04-08, coverage ~0.1%
        "w_retail": 0.0,  # Прод: отключён (live-данные только с 2026-04-08)
        "w_maker_direction": 0.5,  # Прод: taker flow direction (corr +0.76, поднят с 0.2)
        "w_taker_imbalance": 0.0,  # Нет live-данных
    }

    # Категориальные профили (Deep Research v2)
    CATEGORY_PROFILES: dict[str, dict] = {
        "fast_reaction": {
            "oi_median_liquidity": 50_000,
            "categories": ["crypto", "sports", "culture"],
        },
        "high_inertia": {
            "oi_median_liquidity": 500_000,
            "categories": ["politics", "science", "law", "world & economy"],
        },
        "default": {
            "oi_median_liquidity": 100_000,
            "categories": ["tech & ai"],
        },
    }

    # Category-specific min_divergence (calibrated 2026-04-08 on 615K resolved markets)
    # Источник: scripts/research/historical_category_analysis.py → optimism_bias + vwap_deviation
    # Логика: чем эффективнее категория (ниже avg_vwap_deviation), тем выше порог
    # Finance: median_vwap_dev=0.505 (очень efficient) → 0.06
    # Weather: median_vwap_dev=0.920 (крайне неэффicient) → 0.02
    CATEGORY_MIN_DIVERGENCE: dict[str, float] = {
        "weather": 0.02,  # median_vwap_dev=0.920, крайне неэффективный → ловим малые div
        "culture": 0.02,  # median_vwap_dev=0.789, YES-biased
        "crypto": 0.03,  # median_vwap_dev=0.534, средний; 148K markets
        "tech & ai": 0.03,  # median_vwap_dev=0.640
        "sports": 0.04,  # median_vwap_dev=0.683, high variance
        "geopolitics": 0.04,  # median_vwap_dev=0.618, strong taker contra (-0.138)
        "politics": 0.04,  # median_vwap_dev=0.671, strong taker contra (-0.110)
        "science": 0.04,  # нет данных в историческом датасете → default
        "world & economy": 0.05,
        "finance": 0.06,  # median_vwap_dev=0.505, наиболее efficient
        "entertainment": 0.02,
    }
    DEFAULT_MIN_DIVERGENCE: float = 0.03

    # Category-specific Kelly shrinkage (Deep Research 2026-04-08)
    # Research: fractional Kelly (0.25-0.5x) optimal при estimation uncertainty.
    # Baker & McHale 2013: shrinkage растёт с estimation variance.
    CATEGORY_KELLY_SHRINKAGE: dict[str, float] = {
        "culture": 0.50,
        "entertainment": 0.50,
        "weather": 0.40,
        "tech & ai": 0.35,
        "politics": 0.35,
        "science": 0.35,
        "sports": 0.30,
        "crypto": 0.25,
        "finance": 0.15,
    }
    DEFAULT_KELLY_SHRINKAGE: float = 0.35

    # Category-specific retail contra scale (calibrated 2026-04-08 on 615K markets)
    # Источник: scripts/research/historical_category_analysis.py → taker_imbalance_corr с outcome
    # Negative корреляция = retail покупает → outcome противоположный
    # Чем сильнее contra → больше scale для retail_bayes_factor
    CATEGORY_RETAIL_SCALE: dict[str, float] = {
        "geopolitics": 0.45,  # corr -0.138, сильнейший contra
        "politics": 0.40,  # corr -0.110
        "tech & ai": 0.35,  # corr -0.083
        "sports": 0.30,  # corr -0.069
        "crypto": 0.30,  # corr -0.062
        "finance": 0.25,  # corr -0.046
        "culture": 0.15,  # corr +0.041, единственный positive → слабый contra
        "weather": 0.10,  # corr -0.014, почти нет сигнала
    }
    DEFAULT_RETAIL_SCALE: float = 0.30

    # Category-specific divergence clamp (calibrated 2026-04-08 на 615K markets)
    # Источник: avg_vwap_deviation — чем больше optimism bias, тем агрессивнее damping
    # Weather: extreme YES overpricing (avg_vwap_dev=0.72) → почти полностью гасим upward
    # Finance/Crypto: moderate → стандартный damping
    CATEGORY_UPWARD_DAMPING: dict[str, float] = {
        "weather": 0.15,  # avg_vwap_dev=0.72, extreme overpricing
        "culture": 0.20,  # avg_vwap_dev=0.56
        "sports": 0.25,  # avg_vwap_dev=0.46
        "politics": 0.25,  # avg_vwap_dev=0.45
        "geopolitics": 0.30,  # avg_vwap_dev=0.39
        "tech & ai": 0.30,  # avg_vwap_dev=0.42
        "crypto": 0.35,  # avg_vwap_dev=0.24, наименьший bias → больше доверия oracle
        "finance": 0.35,  # avg_vwap_dev=0.29
    }
    DEFAULT_UPWARD_DAMPING: float = 0.30

    CATEGORY_MAX_DIVERGENCE: dict[str, float] = {
        "crypto": 0.12,  # высокая волатильность, больший cap
        "finance": 0.12,  # efficient → edge бывает >10%
        "politics": 0.08,  # inertia, >8% divergence убыточна
        "sports": 0.08,  # bookmaker odds anchor → tight cap
        "weather": 0.06,  # extreme noise, minimal edge
    }
    DEFAULT_MAX_DIVERGENCE: float = 0.10

    def get_category_profile(self, category: str | None) -> dict:
        """Получить профиль категории. Fallback на default."""
        if category:
            for profile_name, profile in self.CATEGORY_PROFILES.items():
                if category.lower() in profile.get("categories", []):
                    return profile
        return self.CATEGORY_PROFILES["default"]

    def get_min_divergence(self, category: str | None) -> float:
        """Category-specific min_divergence threshold."""
        if category:
            return self.CATEGORY_MIN_DIVERGENCE.get(category.lower(), self.DEFAULT_MIN_DIVERGENCE)
        return self.DEFAULT_MIN_DIVERGENCE

    def get_kelly_shrinkage(self, category: str | None) -> float:
        """Category-specific Kelly fraction shrinkage."""
        if category:
            return self.CATEGORY_KELLY_SHRINKAGE.get(category.lower(), self.DEFAULT_KELLY_SHRINKAGE)
        return self.DEFAULT_KELLY_SHRINKAGE

    def get_retail_scale(self, category: str | None) -> float:
        """Category-specific retail contra scale."""
        if category:
            return self.CATEGORY_RETAIL_SCALE.get(category.lower(), self.DEFAULT_RETAIL_SCALE)
        return self.DEFAULT_RETAIL_SCALE

    def get_upward_damping(self, category: str | None) -> float:
        """Category-specific upward divergence damping."""
        if category:
            return self.CATEGORY_UPWARD_DAMPING.get(category.lower(), self.DEFAULT_UPWARD_DAMPING)
        return self.DEFAULT_UPWARD_DAMPING

    def get_max_divergence(self, category: str | None) -> float:
        """Category-specific max divergence cap."""
        if category:
            return self.CATEGORY_MAX_DIVERGENCE.get(category.lower(), self.DEFAULT_MAX_DIVERGENCE)
        return self.DEFAULT_MAX_DIVERGENCE

    def horizon_weight(self, days_to_resolution: float | None) -> float:
        """Множитель divergence по горизонту до resolution.

        Research (Page 2013): markets well-calibrated < 7 дней, biased toward 0.5 > 30 дней.
        Short horizon → trust market more (lower divergence weight).
        Long horizon → trust oracle more (higher divergence weight).
        """
        if days_to_resolution is None:
            return 1.0
        if days_to_resolution <= 7:
            return 0.7
        elif days_to_resolution >= 30:
            return 1.3
        else:
            return 0.7 + 0.6 * (days_to_resolution - 7) / 23

    def ttr_multiplier(self, days_to_resolution: float | None) -> float:
        """Логарифмический boost whale signal по близости к закрытию рынка.

        30 дней → 1.0, 1 день → ~2.0, 1 час → ~2.93, 30 мин → ~3.15.
        """
        if days_to_resolution is None or days_to_resolution > 30:
            return 1.0
        days_clamped = max(days_to_resolution, 0.02)  # ~30 мин minimum
        return 1.0 + math.log(30.0 / days_clamped) / math.log(30.0)

    def __init__(
        self,
        weights: dict[str, float] | None = None,
        f_max: float = F_MAX,
        k_liquidity: float = K_LIQUIDITY,
        expert_weight_enabled: bool = True,
        timing_boost_scale: float = 0.1,
        prior_alpha: float = 1.0,
        prior_beta: float = 1.0,
        credibility_gamma: float = 1.0,
        decay_alpha: float = 0.52,
    ):
        self.weights = weights or self.DEFAULT_WEIGHTS.copy()
        self.f_max = f_max
        self.k_liquidity = k_liquidity
        self.expert_weight_enabled = expert_weight_enabled
        self.timing_boost_scale = timing_boost_scale
        self.prior_alpha = prior_alpha
        self.prior_beta = prior_beta
        self.credibility_gamma = credibility_gamma
        self.decay_alpha = decay_alpha

    # === Sub-step 3.2: Kelly Inversion + Credibility ===

    def informed_credibility(self, wins: int, losses: int) -> float:
        """
        Informed Beta Prior Credibility.

        C = (W + α) / (W + L + α + β)

        При α=5.5, β=4.5 (mean=0.55, strength=10):
        - Новичок: C = 0.55 (стартует со среднего)
        - 12W/5L: C = 0.648
        - 50W/10L: C = 0.793

        При α=1, β=1 → идентично Laplace.
        """
        return (wins + self.prior_alpha) / (wins + losses + self.prior_alpha + self.prior_beta)

    def roi_informed_credibility(self, wins: int, losses: int, roi_percent: float) -> float:
        """
        ROI-Informed Credibility.

        Blend W/L-based credibility с ROI-based через sigmoid + linear transition.

        При W+L < 10:  100% W/L (мало данных для ROI)
        При W+L = 20:  50% ROI + 50% W/L
        При W+L >= 30: 100% ROI

        Sigmoid scale 50:
          roi=0%   → C_roi=0.500
          roi=+50% → C_roi=0.731
          roi=-50% → C_roi=0.269
        """
        c_wl = (wins + self.prior_alpha) / (wins + losses + self.prior_alpha + self.prior_beta)

        total = wins + losses
        if total < 10:
            return c_wl

        c_roi = stable_sigmoid(roi_percent / 50.0)

        roi_weight = max(0.0, min(1.0, (total - 10) / 20.0))
        return roi_weight * c_roi + (1 - roi_weight) * c_wl

    @staticmethod
    def discounted_record(wins: int, losses: int, gamma: float = 0.97, min_trades: int = 20) -> tuple[int, int]:
        """
        Temporal discounting W/L.

        γ=0.97: за 100 сделок вес старейшей = 0.97^100 ≈ 0.048.
        Эффективный размер выборки ≈ 1/(1-γ) при большом N.

        При total < min_trades дисконтирование не применяется —
        для малых выборок все сделки одинаково важны.

        Returns:
            (effective_wins, effective_losses)
        """
        total = wins + losses
        if total == 0 or gamma >= 1.0 or total < min_trades:
            return wins, losses

        effective_n = (1 - gamma**total) / (1 - gamma)
        ratio = effective_n / total
        w_eff = round(wins * ratio)
        l_eff = round(losses * ratio)
        # Гарантировать что минимум 1 при ненулевом оригинале
        if wins > 0 and w_eff == 0:
            w_eff = 1
        if losses > 0 and l_eff == 0:
            l_eff = 1
        return w_eff, l_eff

    def kelly_fraction(self, stake: float, bankroll: float, f_max: float | None = None) -> float:
        """
        Constrained Kelly Fraction.

        §3.2 MATH_SPEC: f = min(S / max(B_est, 1.0), f_max)

        Guard: bankroll <= 0 -> bankroll = 1.0 (предотвращает деление на ноль,
               и при B=1 -> f=S -> capped at f_max).

        Args:
            stake: Размер ставки (S) в USDC
            bankroll: Оценка банкролла (B_est) в USDC
            f_max: Максимальная Kelly fraction. None -> self.f_max.

        Returns:
            Constrained fraction in [0, f_max]
        """
        cap = f_max if f_max is not None else self.f_max
        b = max(bankroll, 1.0)
        return min(stake / b, cap)

    def laplace_credibility(self, wins: int, losses: int, alpha: float = 1.0, beta: float = 1.0) -> float:
        """
        Laplace-Smoothed Credibility (Rule of Succession).

        §3.2 MATH_SPEC: C = (W + alpha) / (W + L + alpha + beta)
        """
        return (wins + alpha) / (wins + losses + alpha + beta)

    def whale_implied_probability(self, f: float, b: float) -> float:
        """
        Uncalibrated Whale Implied Probability.

        §3.2 MATH_SPEC: p_raw = (f * b + 1) / (b + 1)

        Clamped в [eps, 1-eps] для безопасного входа в logit().

        Args:
            f: Constrained Kelly fraction in [0, f_max]
            b: Decimal odds minus 1 = (1/market_price) - 1

        Returns:
            Implied probability in [eps, 1-eps]
        """
        p_raw = (f * b + 1.0) / (b + 1.0)
        return max(EPSILON, min(1.0 - EPSILON, p_raw))

    def whale_bayes_factor(self, whale_input: dict) -> float:
        """
        Полная цепочка: Kelly Fraction -> Implied Probability -> Bayes Factor.

        Принимает dict из WhaleAnalyticsEngine.prepare_bayesian_input().

        §3.2 MATH_SPEC:
            f = min(S / B_est, 0.25)
            p_raw = (f * b + 1) / (b + 1)
            C = (W + 1) / (W + L + 2)
            Z_mkt = logit(market_price)
            Lambda = C * (logit(p_implied_yes) - Z_mkt)

        Для SELL: b вычисляется из NO-odds в prepare_bayesian_input(),
        поэтому p_raw = implied NO probability → p_implied_yes = 1 - p_raw.
        Знак Lambda получается правильным автоматически (отрицательный = медвежий).

        Args:
            whale_input: Dict из prepare_bayesian_input():
                {'S', 'B_est', 'b', 'W', 'L', 'direction', 'current_price', ...}

        Returns:
            Lambda_whale (Bayes Factor). Положительный = бычий сигнал,
            отрицательный = медвежий.
        """
        f_raw = self.kelly_fraction(whale_input["S"], whale_input["B_est"])

        # Confidence-scaled Kelly (рекомендация research §4):
        # При малом track record (<20 bets) — снижаем fraction
        # Half-Kelly default (×0.5) × confidence ramp → осторожнее для новичков
        bet_count = whale_input.get("bet_count", 0)
        track_record = max(bet_count, whale_input["W"] + whale_input["L"])
        confidence_scale = min(1.0, track_record / 20.0) if track_record > 0 else 0.1
        f = f_raw * confidence_scale

        b = whale_input["b"]
        p_raw = self.whale_implied_probability(f, b)

        # Для SELL: p_raw — implied NO probability, конвертируем в YES-пространство
        if whale_input.get("direction", "buy") == "sell":
            p_implied_yes = 1.0 - p_raw
        else:
            p_implied_yes = p_raw

        # Credibility: pre-computed (Goldsky PnL, v2.1) или из W/L
        if "credibility" in whale_input and whale_input["credibility"] is not None:
            c = whale_input["credibility"]
        else:
            # Temporal discounting W/L (γ < 1.0 → старые сделки теряют вес)
            w_eff, l_eff = self.discounted_record(
                whale_input["W"],
                whale_input["L"],
                gamma=self.credibility_gamma,
            )
            roi_pct = whale_input.get("roi_percent", 0.0)
            c = self.roi_informed_credibility(w_eff, l_eff, roi_pct)

        z_mkt = safe_logit(whale_input["current_price"])
        z_whale = safe_logit(p_implied_yes)

        # Phase 1.3: expert_weight модифицирует силу сигнала
        # expert_weight [0, 1] → expert_multiplier [0.5, 1.5]
        # При default 0.5 → multiplier=1.0 (identity, без изменений)
        if self.expert_weight_enabled:
            ew = whale_input.get("expert_weight", 0.5)
            expert_multiplier = 0.5 + ew
        else:
            expert_multiplier = 1.0

        # Phase 1.4: timing_score как credibility modifier
        # timing_score [-1, 1] → boost [-scale, +scale]
        # При default 0 → boost=0 (без изменений)
        timing = whale_input.get("timing_score", 0.0)
        timing_boost = timing * self.timing_boost_scale
        c_adjusted = max(0.05, min(0.95, c + timing_boost))

        # Market Impact credibility boost (Фаза 3 Market Impact Score):
        # impact_6h > 0 → рынок двигается в сторону кита → усиление credibility
        # impact_6h < 0 → рынок двигается против → ослабление
        # k = 2.0: impact_6h = +0.05 → C *= 1.10 (10% boost)
        impact_6h = whale_input.get("market_impact_6h")
        if impact_6h is not None:
            impact_boost = max(-0.3, min(0.3, float(impact_6h) * 2.0))
            c_adjusted = max(0.05, min(0.95, c_adjusted * (1.0 + impact_boost)))

        # Phase 3: Whale Profile Multiplier (ROI + experience + sector + style)
        # Проверенные прибыльные киты усиливаются, убыточные гасятся
        profile_mult = whale_profile_multiplier(
            roi_percent=whale_input.get("roi_percent", 0.0),
            bet_count=whale_input.get("bet_count", 0),
            sector_win_rates=whale_input.get("sector_win_rates"),
            market_category=whale_input.get("market_category"),
            whale_style=whale_input.get("whale_style"),
            wins=whale_input["W"],
            losses=whale_input["L"],
        )

        # Знак Lambda автоматически правильный:
        # BUY: p_implied_yes > market → Lambda > 0 (бычий)
        # SELL: p_implied_yes < market → Lambda < 0 (медвежий)
        lam = c_adjusted * expert_multiplier * profile_mult * (z_whale - z_mkt)

        return lam

    # === Sub-step 3.3: Liquidity Gating ===

    def liquidity_gate(
        self,
        lambda_whale: float,
        spread: float | None,
        depth: float | None,
        volume: float | None,
        k: float | None = None,
    ) -> float:
        """
        Microstructure Liquidity Gating.

        §3.3 MATH_SPEC:
            delta = k / max(s, eps)
            Lambda_gated = Lambda * tanh(delta * L/V)

        Если данные microstructure недоступны (None):
            -> Lambda_gated = Lambda * 0.5 (консервативный fallback: 50% сигнала)

        Args:
            lambda_whale: Bayes Factor кита (Lambda_whale) из whale_bayes_factor()
            spread: Bid-ask spread (s). None если недоступен.
            depth: Order book depth на соответствующей стороне (L). None если недоступен.
            volume: Объём ставки (V). None если недоступен.
            k: Tuning parameter (default 0.01)

        Returns:
            Lambda_gated — ослабленный (или полный) Bayes Factor
        """
        # Fallback при отсутствии данных
        if spread is None or depth is None or volume is None:
            return lambda_whale * 0.5

        # Guard: volume <= 0 -> fallback
        if volume <= 0:
            return lambda_whale * 0.5

        k_val = k if k is not None else self.k_liquidity
        delta = k_val / max(spread, EPSILON)
        gate = math.tanh(delta * depth / volume)

        return lambda_whale * gate

    # === Sub-step 3.4: Temporal News Decay ===

    def temporal_decay(
        self,
        lambda_initial: float,
        hours_elapsed: float,
        half_life_hours: float = 24.0,
    ) -> float:
        """
        Temporal News Decay.

        decay_alpha > 0: Power Law — λ(t) = λ₀ / (1 + t/τ)^α
            τ = half_life / (2^(1/α) - 1) чтобы при t=half_life → 50%.
            Свойство: быстрый спад + длинный хвост.

        decay_alpha = 0: Exponential (legacy) — λ(t) = λ₀ × e^(-ln2/H × t)
        """
        if half_life_hours <= 0:
            return 0.0

        t = max(hours_elapsed, 0.0)

        if self.decay_alpha > 0:
            tau = half_life_hours / (2 ** (1 / self.decay_alpha) - 1)
            return lambda_initial / (1 + t / tau) ** self.decay_alpha
        else:
            decay_rate = math.log(2) / half_life_hours
            return lambda_initial * math.exp(-decay_rate * t)

    # === Open Interest Bayes Factor ===

    def oi_bayes_factor(
        self,
        liquidity: float,
        volume: float,
        median_liquidity: float = DEFAULT_MEDIAN_LIQUIDITY,
        scale: float = OI_SCALE,
    ) -> float:
        """
        Open Interest Bayes Factor.

        Λ_oi = scale × log(liquidity / median_liquidity)

        High liquidity → market attracts attention → positive signal.
        Low liquidity → illiquid market → negative signal.

        Args:
            liquidity: Текущая ликвидность рынка (USD).
            volume: Объём торгов (для будущего использования).
            median_liquidity: Медианная ликвидность ($100K default).
            scale: Масштабный коэффициент (0.3 default).

        Returns:
            Bayes Factor, clamped to [-1.0, 1.0].
        """
        if liquidity <= 0:
            return -1.0

        ratio = liquidity / median_liquidity
        lam = scale * math.log(ratio)
        return max(-1.0, min(1.0, lam))

    # === Concentration Bayes Factor ===

    def concentration_bayes_factor(
        self,
        top5_concentration: float,
        total_holders: int,
        baseline: float = CONCENTRATION_BASELINE,
        scale: float = CONCENTRATION_SCALE,
        min_holders: int = MIN_HOLDERS_FOR_SIGNAL,
        concentration_yes: float | None = None,
        concentration_no: float | None = None,
    ) -> float:
        """Whale Concentration Bayes Factor.

        Если доступны directional данные (concentration_yes, concentration_no) —
        используется directional signal. Иначе — legacy формула.

        Returns:
            Bayes Factor, clamped to [-0.5, 0.5].
        """
        if total_holders < min_holders:
            return 0.0

        if concentration_yes is not None and concentration_no is not None:
            # Directional: YES доминирует → positive
            lam = scale * (concentration_yes - concentration_no)
        else:
            # Legacy: общая концентрация vs baseline
            lam = scale * (top5_concentration - baseline)

        return max(-0.5, min(0.5, lam))

    # === Retail Flow Bayes Factor ===

    @staticmethod
    def retail_bayes_factor(
        retail_buy_count: int,
        retail_sell_count: int,
        whale_direction_sign: float,
        scale: float = 0.3,
    ) -> float:
        """Retail contra-signal: fade retail when they disagree with whale consensus.

        Логика: если retail массово покупает (buy_ratio > 0.5), а киты продают
        (whale_direction_sign < 0), retail ошибается — сигнал усиливает whale direction.

        Args:
            retail_buy_count: Кол-во retail BUY трейдов за окно (4-24ч).
            retail_sell_count: Кол-во retail SELL трейдов за окно.
            whale_direction_sign: +1 если whale consensus = YES, -1 если NO, 0 если нет whale data.
            scale: Максимальная амплитуда сигнала.

        Returns:
            Bayes Factor ∈ [-0.3, 0.3]. Положительный = retail agrees (усиливает), отрицательный = contra.
        """
        total = retail_buy_count + retail_sell_count
        if total < 10 or whale_direction_sign == 0:
            return 0.0

        # Retail bias: >0.5 = retail покупает YES, <0.5 = retail продаёт
        retail_buy_ratio = retail_buy_count / total

        # Отклонение от 50/50: [-0.5, 0.5]
        retail_bias = retail_buy_ratio - 0.5

        # Contra: retail bias * whale direction sign
        # Если retail покупает (+) а whale продаёт (-) → negative = contra signal
        # Если retail покупает (+) а whale покупает (+) → positive = agreement
        lam = scale * retail_bias * whale_direction_sign

        return max(-0.3, min(0.3, lam))

    # === Volatility Bayes Factor ===

    # Замечание (2026-04-10): бывший `microstructure_bayes_factor` удалён.
    # Формула (depth_yes - depth_no) / (depth_yes + depth_no) исторически
    # считала (bid - ask) / (bid + ask) одного token_id (MM asymmetry →
    # systematic bias ≈ -0.82). Правильный сигнал — taker flow через
    # data-api /trades, см. app/jobs/clob_book_sync.py и
    # maker_direction_bayes_factor ниже.

    def maker_direction_bayes_factor(
        self,
        maker_direction_ratio: float,
        scale: float = 0.8,
    ) -> float:
        """Maker Direction Bayes Factor из CLOB /book агрегатов (Phase 2).

        maker_direction_ratio = (bid_usd - ask_usd) / (bid_usd + ask_usd) ∈ [-1, 1]
        Положительный → мейкеры на стороне YES → бычий сигнал (corr +0.75 с outcome).

        Отличие от microstructure_bayes_factor: использует исторические агрегаты
        из maker_direction_cache (TTL 2h), не live WebSocket.

        Args:
            maker_direction_ratio: Нормализованный дисбаланс мейкеров [-1, 1].
            scale: Масштаб (0.8 = тот же что у microstructure).

        Returns:
            Bayes Factor, clamped to [-0.8, 0.8].
        """
        lam = scale * maker_direction_ratio
        return max(-0.8, min(0.8, lam))

    @staticmethod
    def taker_imbalance_bayes_factor(
        buy_ratio: float,
        scale: float = 0.15,
    ) -> float:
        """Taker Imbalance Bayes Factor — нелинейный contrarian сигнал (Phase 2).

        СТАТУС (2026-04-10): активный фактор, но **по умолчанию отключён**
        (w_taker_imbalance = 0.0 во всех активных calibration_configs).
        Причина: источник данных `market_signals.taker_buy_volume_usd` наполняется
        только через `historical_backfill` (/mnt/data/polymarket_dataset) — для
        live окна этот сигнал не обновляется, поэтому на проде всегда buy_ratio=None.

        Настоящее направление умных денег правильнее считать через taker_bias
        по obvcomeIndex (token1 vs token2), см. `app/jobs/clob_book_sync.py` —
        этот сигнал идёт через maker_direction_bayes_factor (не taker_imbalance).

        Research (historical dataset, 615K markets):
        - buy_mild (0.55-0.70): corr -0.135 с outcome (розничные покупатели ошибаются)
        - Нелинейность: extreme zones менее предсказуемы чем mild.
        - Нейтральная зона (0.45-0.55): нет сигнала.

        Args:
            buy_ratio: Доля buy-транзакций тейкеров [0, 1].
            scale: Максимальная амплитуда (0.15).

        Returns:
            Bayes Factor ∈ [-0.3, 0.3]. Отрицательный = fade retail buyers.
        """
        # Нейтральная зона
        if 0.45 <= buy_ratio <= 0.55:
            return 0.0

        # Deviation from neutral (50%)
        bias = buy_ratio - 0.5  # [-0.5, 0.5]

        # Нелинейность: mild zone (-scale), extreme zone уменьшаем вдвое
        abs_bias = abs(bias)
        if abs_bias > 0.30:
            effective_scale = scale * 0.5  # extreme → ослабляем
        else:
            effective_scale = scale

        # Contrarian: retail покупает → fade (отрицательный сигнал)
        lam = -effective_scale * (bias / 0.5)  # нормализуем к [-1, 1]

        return max(-0.3, min(0.3, lam))

    def horizon_adjustment_multiplier(
        self,
        days_to_resolution: float | None,
    ) -> float:
        """Horizon Adjustment: множитель EV по горизонту до resolution (Phase 2).

        Research (historical dataset): EV/$ sweet spot = 14-30 дней.
        - <7 дней: рынок хорошо откалиброван, edge минимален → 0.7x
        - 7-14 дней: линейный рост от 0.7 до 1.0
        - 14-30 дней: sweet spot, EV/$ +0.040 → 1.0-1.2x
        - >30 дней: неопределённость растёт → снижаем до 1.0x

        Отличие от horizon_weight(): тот модулирует divergence, этот —
        общую силу oracle signal для execution decisions.

        Returns:
            Multiplier in [0.7, 1.2]. 1.0 = нейтрально.
        """
        if days_to_resolution is None:
            return 1.0
        if days_to_resolution <= 7:
            return 0.7
        elif days_to_resolution <= 14:
            # Линейный рост 0.7 → 1.0
            return 0.7 + 0.3 * (days_to_resolution - 7) / 7
        elif days_to_resolution <= 30:
            # Sweet spot: линейный рост 1.0 → 1.2
            return 1.0 + 0.2 * (days_to_resolution - 14) / 16
        else:
            # >30d: более быстрое снижение (неопределённость растёт)
            return max(0.9, 1.2 - 0.2 * (days_to_resolution - 30) / 30)

    @staticmethod
    def calculate_vpin(
        trades: list[dict],
        n_buckets: int = 10,
        bucket_volume: float | None = None,
    ) -> float | None:
        """Volume-Synchronized Probability of Informed Trading (Easley, López de Prado, O'Hara).

        Args:
            trades: [{size, side}] где side ∈ {"buy", "sell"}.
            n_buckets: Количество volume buckets.
            bucket_volume: Объём одного bucket. None = total / n_buckets.

        Returns:
            VPIN ∈ [0, 1] или None если данных недостаточно.
        """
        if not trades or len(trades) < 2 * n_buckets:
            return None

        total_volume = sum(float(t.get("size", 0) or t.get("amount", 0)) for t in trades)
        if total_volume <= 0:
            return None

        bv = bucket_volume or (total_volume / n_buckets)
        if bv <= 0:
            return None

        imbalances: list[float] = []
        v_buy = 0.0
        v_sell = 0.0
        bucket_fill = 0.0

        for t in trades:
            size = float(t.get("size", 0) or t.get("amount", 0))
            side = str(t.get("side", t.get("direction", "buy"))).lower()

            remaining = size
            while remaining > 0:
                space = bv - bucket_fill
                fill = min(remaining, space)

                if side == "buy":
                    v_buy += fill
                else:
                    v_sell += fill

                bucket_fill += fill
                remaining -= fill

                if bucket_fill >= bv - 1e-9:
                    total_bucket = v_buy + v_sell
                    if total_bucket > 0:
                        imbalances.append(abs(v_buy - v_sell) / total_bucket)
                    v_buy = 0.0
                    v_sell = 0.0
                    bucket_fill = 0.0

        if not imbalances:
            return None

        return sum(imbalances) / len(imbalances)

    # === Sub-step 3.5: Bayesian Aggregation ===

    def calculate_oracle_score(
        self,
        market_price: float,
        whale_factors: list[float],
        weights: dict[str, float] | None = None,
        temperature: float = 1.3,  # Calibrated 2026-04-08: T=1.3 optimal on 278K markets walk-forward
        smart_money_factors: dict[str, list[float]] | None = None,
        oi_factor: float = 0.0,
        concentration_factor: float = 0.0,
        micro_factor: float = 0.0,
        sports_odds_factor: float = 0.0,
        retail_factor: float = 0.0,
        # Phase 2: новые факторы
        maker_direction_factor: float = 0.0,
        taker_imbalance_factor: float = 0.0,
        # Deep Research v2:
        calibration_method: str = "temperature",
        beta_a: float = 1.0,
        beta_b: float = 0.0,
        category: str | None = None,
        shadow_market_id: str | None = None,
        shadow_side: str = "YES",
    ) -> dict:
        """
        Главный метод: Bayesian Log-Odds Oracle Score.

        §3.5 MATH_SPEC:
            Z_posterior = w_mkt * Z_mkt + sum(w_whale * Lambda_i)
                        + w_oi * Lambda_oi + w_conc * Lambda_conc + w_micro * Lambda_micro
                        + w_retail * Lambda_retail
            P_oracle = sigma(Z_posterior / T)

        Args:
            market_price: Текущая рыночная цена [0, 1] — prior
            whale_factors: Список gated Bayes Factors китов (уже прошли liquidity_gate)
            weights: Опциональные веса {w_mkt, w_whale}. None -> DEFAULT_WEIGHTS.
            temperature: Temperature scaling (T > 0). T=1.0 — без изменений.
            oi_factor: Open Interest Bayes Factor (из oi_bayes_factor()).
            concentration_factor: Whale Concentration Bayes Factor (из concentration_bayes_factor()).
            micro_factor: Устаревший Microstructure Bayes Factor (функция удалена
                2026-04-10, параметр оставлен для backward compat — всегда 0.0).
            retail_factor: Retail Flow Bayes Factor (из retail_bayes_factor()).

        Returns:
            Dict с компонентами Oracle Score
        """
        w = weights or self.weights
        t = temperature if temperature > 0 else 1.0

        # Market prior в log-odds
        z_mkt = safe_logit(market_price)

        # Raw contributions (unweighted sums — для grid search калибровки)
        whale_contribution_raw = sum(whale_factors)

        # Whale interaction term (research: whale disagree = 91-97% accuracy)
        sign_z = 1.0 if z_mkt >= 0 else -1.0
        whale_contra_raw = whale_contribution_raw * sign_z
        DW = BayesianScoreCalculator.DEFAULT_WEIGHTS
        whale_contra_contribution = w.get("w_whale_contra", DW["w_whale_contra"]) * whale_contra_raw

        # Weighted contributions
        whale_contribution = w.get("w_whale", DW["w_whale"]) * whale_contribution_raw

        # Smart Money contributions
        sm = smart_money_factors or {}
        sm_fresh_raw = sum(sm.get("fresh_wallet", sm.get("fresh", [])))
        sm_cluster_raw = sum(sm.get("cluster", []))
        sm_registry_raw = sum(sm.get("registry", []))

        sm_fresh_contribution = w.get("w_fresh", DW["w_fresh"]) * sm_fresh_raw
        sm_cluster_contribution = w.get("w_cluster", DW["w_cluster"]) * sm_cluster_raw
        sm_registry_contribution = w.get("w_registry", DW["w_registry"]) * sm_registry_raw

        # Open Interest, Concentration, Microstructure contributions
        oi_contribution = w.get("w_oi", DW["w_oi"]) * oi_factor
        concentration_contribution = w.get("w_concentration", DW["w_concentration"]) * concentration_factor
        micro_contribution = w.get("w_micro", DW["w_micro"]) * micro_factor
        sports_odds_contribution = w.get("w_sports_odds", DW["w_sports_odds"]) * sports_odds_factor
        retail_contribution = w.get("w_retail", DW["w_retail"]) * retail_factor
        maker_direction_contribution = w.get("w_maker_direction", DW["w_maker_direction"]) * maker_direction_factor
        taker_imbalance_contribution = w.get("w_taker_imbalance", DW["w_taker_imbalance"]) * taker_imbalance_factor

        # Log-Linear Aggregation
        # cluster_discount (per-whale multiplier) и sm_cluster (SmartMoneyEngine signal) — независимые сигналы
        z_posterior = (
            w.get("w_mkt", DW["w_mkt"]) * z_mkt
            + whale_contribution
            + whale_contra_contribution
            + sm_fresh_contribution
            + sm_cluster_contribution
            + sm_registry_contribution
            + oi_contribution
            + concentration_contribution
            + micro_contribution
            + sports_odds_contribution
            + retail_contribution
            + maker_direction_contribution
            + taker_imbalance_contribution
        )

        # Обратно в вероятность (Deep Research v2: beta или temperature)
        if calibration_method == "beta":
            p_raw = stable_sigmoid(beta_a * z_posterior + beta_b)
        else:
            p_raw = stable_sigmoid(z_posterior / t)

        # Asymmetric divergence clamp (category-aware):
        # - Upward (oracle > market): dampen — исторически Brier +0.024
        # - Max divergence cap per category — >10% убыточен в большинстве категорий
        up_damping = self.get_upward_damping(category)
        max_div = self.get_max_divergence(category)
        divergence = p_raw - market_price
        if divergence > 0:
            divergence *= up_damping
        clamped_div = max(-max_div, min(max_div, divergence))
        oracle_score = max(EPSILON, min(1 - EPSILON, market_price + clamped_div))

        # Phase 2 shadow mode — call pure scoring in parallel if enabled
        try:
            from app.core.config import get_settings

            if get_settings().scoring_pure_enabled:
                from datetime import UTC, datetime

                from app.models.feature_vector import FeatureVector
                from app.services.shadow_scoring import compare_scores_for_snapshot

                shadow_fv = FeatureVector(
                    market_id=shadow_market_id or "unknown",
                    snapshot_at=datetime.now(UTC),
                    side=shadow_side,
                    market_price=market_price,
                    source="live",
                    whale_contribution_raw=whale_contribution_raw,
                    oi_factor_raw=oi_factor,
                    concentration_factor_raw=concentration_factor,
                    micro_factor_raw=micro_factor,
                    maker_direction_raw=maker_direction_factor,
                    taker_imbalance_raw=taker_imbalance_factor,
                )
                shadow_legacy_score = float(oracle_score)
                try:
                    loop = asyncio.get_running_loop()
                    shadow_task = loop.create_task(compare_scores_for_snapshot(shadow_fv, shadow_legacy_score))
                    _shadow_tasks.add(shadow_task)
                    shadow_task.add_done_callback(_shadow_tasks.discard)
                except RuntimeError:
                    # No running loop (shouldn't happen in live, but safe fallback) — skip
                    pass
        except Exception as e:
            import structlog

            structlog.get_logger().warning("shadow_scoring_failed", error=str(e))

        return {
            "oracle_score": oracle_score,
            "z_posterior": z_posterior,
            "p_raw": p_raw,
            "p_swan": 0.0,
            "z_mkt": z_mkt,
            "w_mkt": w.get("w_mkt", DW["w_mkt"]),
            "temperature": t,
            "whale_contribution": whale_contribution,
            "whale_contribution_raw": whale_contribution_raw,
            "whale_contra_raw": whale_contra_raw,
            "whale_contra_contribution": whale_contra_contribution,
            "n_whales": len(whale_factors),
            "sm_fresh_contribution": sm_fresh_contribution,
            "sm_cluster_contribution": sm_cluster_contribution,
            "sm_registry_contribution": sm_registry_contribution,
            "sm_fresh_raw": sm_fresh_raw,
            "sm_cluster_raw": sm_cluster_raw,
            "sm_registry_raw": sm_registry_raw,
            "oi_contribution": oi_contribution,
            "concentration_contribution": concentration_contribution,
            "micro_contribution": micro_contribution,
            "oi_factor_raw": oi_factor,
            "concentration_factor_raw": concentration_factor,
            "micro_factor_raw": micro_factor,
            "sports_odds_raw": sports_odds_factor,
            "sports_odds_contribution": sports_odds_contribution,
            "retail_factor_raw": retail_factor,
            "retail_contribution": retail_contribution,
            "maker_direction_factor_raw": maker_direction_factor,
            "maker_direction_contribution": maker_direction_contribution,
            "taker_imbalance_factor_raw": taker_imbalance_factor,
            "taker_imbalance_contribution": taker_imbalance_contribution,
            "calibration_method": calibration_method,
            "beta_a": beta_a,
            "beta_b": beta_b,
        }

    def calculate_full_score(
        self,
        market_price: float,
        whale_inputs: list[dict],
        microstructure: dict | None,
        temperature: float = 1.3,  # Calibrated 2026-04-08: T=1.3 optimal on 278K markets walk-forward
        smart_money_signals: dict[str, list[float]] | None = None,
        oi_data: dict | None = None,
        holders_data: dict | None = None,
        # Deep Research v2:
        category: str | None = None,
        days_to_resolution: float | None = None,
        median_microstructure: dict | None = None,
        vpin: float | None = None,
        beta_a: float = 1.0,
        beta_b: float = 0.0,
        calibration_method: str = "temperature",
        cluster_discounts: dict[str, float] | None = None,
        sports_odds_factor: float = 0.0,
        retail_factor: float = 0.0,
        # Phase 2: сырые значения из maker_direction_cache / market_signals
        maker_direction_ratio: float | None = None,
        taker_buy_ratio: float | None = None,
        shadow_market_id: str | None = None,
        shadow_side: str = "YES",
    ) -> dict:
        """
        End-to-end расчёт: от сырых данных до P_oracle.

        Оркестрирует всю цепочку:
        1. Для каждого whale_input: whale_bayes_factor() -> liquidity_gate() -> Lambda_gated
        2. calculate_oracle_score(market_price, [Lambda_gated...], T)

        Args:
            market_price: Текущая цена рынка [0, 1]
            whale_inputs: Список dict'ов из prepare_bayesian_input()
            microstructure: {'spread', 'depth_yes', 'depth_no'} из Redis cache. None -> fallback.
            temperature: Temperature scaling (T > 0). T=1.0 — без изменений.

        Returns:
            Расширенный dict (всё из calculate_oracle_score + детали whale)
        """
        # Whale Bayes Factors + Liquidity Gating
        whale_factors = []
        whale_details = []
        for wi in whale_inputs:
            lam_raw = self.whale_bayes_factor(wi)

            # Определяем depth по стороне сделки
            spread = microstructure.get("spread") if microstructure else None
            if microstructure:
                outcome = wi.get("outcome", "YES")
                depth = microstructure.get("depth_yes") if outcome == "YES" else microstructure.get("depth_no")
            else:
                depth = None

            lam_gated = self.liquidity_gate(lam_raw, spread, depth, wi.get("S"))

            # TTR scaling (Deep Research v2)
            ttr_mult = self.ttr_multiplier(days_to_resolution)
            lam_gated *= ttr_mult

            # Cluster discount (v2)
            if cluster_discounts:
                address = wi.get("whale_address", "")
                cd = cluster_discounts.get(address.lower(), 1.0)
                lam_gated *= cd

            whale_factors.append(lam_gated)
            whale_details.append(
                {
                    "address": wi.get("whale_address", ""),
                    "lambda_raw": lam_raw,
                    "lambda_gated": lam_gated,
                    "profile_multiplier": whale_profile_multiplier(
                        roi_percent=wi.get("roi_percent", 0.0),
                        bet_count=wi.get("bet_count", 0),
                        sector_win_rates=wi.get("sector_win_rates"),
                        market_category=wi.get("market_category"),
                        whale_style=wi.get("whale_style"),
                        wins=wi.get("W", 0),
                        losses=wi.get("L", 0),
                    ),
                    "roi_percent": round(wi.get("roi_percent", 0.0), 2),
                    "win_rate": round(wi["W"] / max(wi["W"] + wi["L"], 1), 3),
                }
            )

        # Category-specific profile (Deep Research v2)
        profile = self.get_category_profile(category)
        cat_median_liquidity = profile["oi_median_liquidity"]

        # OI Bayes Factor
        oi_factor = 0.0
        if oi_data:
            oi_factor = self.oi_bayes_factor(
                liquidity=oi_data.get("liquidity", 0),
                volume=oi_data.get("volume", 0),
                median_liquidity=cat_median_liquidity,
            )
            # v2.3: Subsidized markets (sampling rewards) — ликвидность не органическая
            if oi_data.get("is_subsidized"):
                oi_factor *= 0.5

        # Concentration Bayes Factor
        concentration_factor = 0.0
        if holders_data:
            concentration_factor = self.concentration_bayes_factor(
                top5_concentration=holders_data.get("top5_concentration", 0),
                total_holders=int(holders_data.get("total_holders", 0)),
                concentration_yes=holders_data.get("concentration_yes"),
                concentration_no=holders_data.get("concentration_no"),
            )

        # Microstructure Bayes Factor удалён (audit 2026-04-10): формула была
        # семантически некорректна (bid/ask одного token_id, не YES vs NO).
        # Параметры microstructure / median_microstructure / vpin всё ещё
        # принимаются — они используются для liquidity_gate выше и могут
        # быть полезны для будущих фич.
        _ = (median_microstructure, vpin)
        micro_factor = 0.0

        # Phase 2: Maker Direction Bayes Factor (из maker_direction_cache)
        maker_direction_factor = 0.0
        if maker_direction_ratio is not None:
            maker_direction_factor = self.maker_direction_bayes_factor(maker_direction_ratio)

        # Phase 2: Taker Imbalance Bayes Factor (из market_signals)
        taker_imbalance_factor = 0.0
        if taker_buy_ratio is not None:
            taker_imbalance_factor = self.taker_imbalance_bayes_factor(taker_buy_ratio)

        # Финальная агрегация
        result = self.calculate_oracle_score(
            market_price,
            whale_factors,
            temperature=temperature,
            smart_money_factors=smart_money_signals,
            oi_factor=oi_factor,
            concentration_factor=concentration_factor,
            micro_factor=micro_factor,
            calibration_method=calibration_method,
            beta_a=beta_a,
            beta_b=beta_b,
            sports_odds_factor=sports_odds_factor,
            retail_factor=retail_factor,
            maker_direction_factor=maker_direction_factor,
            taker_imbalance_factor=taker_imbalance_factor,
            category=category,
            shadow_market_id=shadow_market_id,
            shadow_side=shadow_side,
        )
        result["whale_details"] = whale_details
        result["n_holders"] = int(holders_data.get("total_holders", 0)) if holders_data else 0

        # Cluster discount metadata
        discounted_count = 0
        discount_sum = 0.0
        discounted_addrs = []
        if cluster_discounts:
            for addr, cd in cluster_discounts.items():
                if cd < 1.0:
                    discounted_count += 1
                    discount_sum += cd
                    discounted_addrs.append(addr)

        result["cluster_discounts_applied"] = discounted_count
        result["avg_cluster_discount"] = discount_sum / max(discounted_count, 1)
        result["discounted_whale_addresses"] = discounted_addrs

        return result

    @staticmethod
    def recalculate_from_raw(
        market_price: float,
        whale_raw: float,
        w_mkt: float,
        w_whale: float,
        temperature: float,
        *,
        oi_raw: float = 0.0,
        concentration_raw: float = 0.0,
        micro_raw: float = 0.0,
        sm_fresh_raw: float = 0.0,
        sm_cluster_raw: float = 0.0,
        sm_registry_raw: float = 0.0,
        w_oi: float = DEFAULT_WEIGHTS["w_oi"],
        w_concentration: float = DEFAULT_WEIGHTS["w_concentration"],
        w_micro: float = DEFAULT_WEIGHTS["w_micro"],
        w_fresh: float = DEFAULT_WEIGHTS["w_fresh"],
        w_cluster: float = DEFAULT_WEIGHTS["w_cluster"],
        w_registry: float = DEFAULT_WEIGHTS["w_registry"],
        w_whale_contra: float = DEFAULT_WEIGHTS["w_whale_contra"],
        calibration_method: str = "beta",
        beta_a: float = 1.7719,
        beta_b: float = -0.2684,
    ) -> float:
        """
        Пересчёт oracle_score из сохранённых raw contributions.

        Чистая математика для grid search калибровки. Формула:
            Z = w_mkt * logit(market_price) + w_whale * whale_raw
              + w_whale_contra * (whale_raw * sign(z_mkt))
              + w_oi * oi_raw + w_conc * conc_raw + w_micro * micro_raw
              + w_fresh * sm_fresh + w_cluster * sm_cluster + w_registry * sm_registry
            P = sigma(Z / T)

        Backward compatible: новые параметры keyword-only с дефолтами.
        """
        t = temperature if temperature > 0 else 1.0
        z_mkt = safe_logit(market_price)
        sign_z = 1.0 if z_mkt >= 0 else -1.0
        z_posterior = (
            w_mkt * z_mkt
            + w_whale * whale_raw
            + w_whale_contra * (whale_raw * sign_z)
            + w_oi * oi_raw
            + w_concentration * concentration_raw
            + w_micro * micro_raw
            + w_fresh * sm_fresh_raw
            + w_cluster * sm_cluster_raw
            + w_registry * sm_registry_raw
        )
        if calibration_method == "beta":
            p_raw = stable_sigmoid(beta_a * z_posterior + beta_b)
        else:
            p_raw = stable_sigmoid(z_posterior / t)

        # Asymmetric divergence clamp (аналогично calculate_oracle_score)
        divergence = p_raw - market_price
        if divergence > 0:
            divergence *= UPWARD_DAMPING
        clamped_div = max(-MAX_DIVERGENCE, min(MAX_DIVERGENCE, divergence))
        return max(EPSILON, min(1 - EPSILON, market_price + clamped_div))

    # === compute_decomposition — Score Decomposition для прозрачности UI ===

    @staticmethod
    def compute_decomposition(
        market_price: float,
        result: dict,
    ) -> dict:
        """
        Вычислить инкрементальную декомпозицию Oracle Score для UI.

        Учитывает w_mkt и temperature из калибровки (приходят в result dict).

        Математика:
        - z_base = w_mkt * logit(market_price)
        - market_prior = market_price (UI-friendly)
        - whale_shift = sigmoid((z_base + whale) / T) - sigmoid(z_base / T)

        Args:
            market_price: Текущая рыночная цена [0, 1]
            result: Dict из calculate_full_score() (включает w_mkt, temperature)

        Returns:
            Dict для JSONB score_decomposition
        """
        z_mkt = result.get("z_mkt", safe_logit(market_price))
        w_mkt = result.get("w_mkt", 1.0)
        temperature = result.get("temperature", 1.0)
        whale_contribution = result.get("whale_contribution", 0.0)
        oracle_score = result.get("oracle_score", market_price)
        p_raw = result.get("p_raw", oracle_score)

        # z_base учитывает вес рыночной цены из калибровки
        z_base = w_mkt * z_mkt

        # Инкрементальные probability shifts с учётом temperature
        p_after_whale = stable_sigmoid((z_base + whale_contribution) / temperature)

        whale_shift = p_after_whale - market_price

        # Top 5 китов по abs(lambda_gated), адреса маскированы
        # Конвертируем в probability shift (не log-odds) для корректного отображения в %
        whale_details = result.get("whale_details", [])
        sorted_whales = sorted(whale_details, key=lambda w: abs(w.get("lambda_gated", 0)), reverse=True)[:5]

        top_whales = []
        cumulative_lambda = 0.0
        for w in sorted_whales:
            addr = w.get("address", "")
            if len(addr) >= 10:
                masked = f"{addr[:6]}...{addr[-4:]}"
            else:
                masked = addr

            lam = w.get("lambda_gated", 0.0)
            # Инкрементальный probability shift для каждого кита
            w_whale = result.get("whale_contribution", 0.0) / max(result.get("whale_contribution_raw", 1.0), EPSILON)
            p_before = stable_sigmoid((z_base + w_whale * cumulative_lambda) / temperature)
            cumulative_lambda += lam
            p_after = stable_sigmoid((z_base + w_whale * cumulative_lambda) / temperature)
            prob_shift = p_after - p_before

            top_whales.append(
                {
                    "address": masked,
                    "shift": round(prob_shift, 4),
                    "direction": "bullish" if lam >= 0 else "bearish",
                    "roi_percent": w.get("roi_percent", 0.0),
                    "win_rate": w.get("win_rate", 0.0),
                }
            )

        # Smart Money shifts (агрегированный + компоненты)
        sm_fresh = result.get("sm_fresh_contribution", 0.0)
        sm_cluster = result.get("sm_cluster_contribution", 0.0)
        sm_registry = result.get("sm_registry_contribution", 0.0)

        z_before_sm = z_base + whale_contribution
        p_before_sm = stable_sigmoid(z_before_sm / temperature)
        p_after_sm = stable_sigmoid((z_before_sm + sm_fresh + sm_cluster + sm_registry) / temperature)
        fresh_wallet_shift = 0.0
        cluster_shift = 0.0
        registry_shift = 0.0
        smart_money_shift = p_after_sm - p_before_sm
        if abs(sm_fresh + sm_cluster + sm_registry) > EPSILON:
            total_sm = sm_fresh + sm_cluster + sm_registry
            if abs(total_sm) > EPSILON:
                fresh_wallet_shift = smart_money_shift * sm_fresh / total_sm
                cluster_shift = smart_money_shift * sm_cluster / total_sm
                registry_shift = smart_money_shift * sm_registry / total_sm

        # OI, Concentration, Microstructure shifts
        oi_contribution = result.get("oi_contribution", 0.0)
        concentration_contribution = result.get("concentration_contribution", 0.0)
        micro_contribution = result.get("micro_contribution", 0.0)

        z_before_oi = z_before_sm + sm_fresh + sm_cluster + sm_registry
        p_before_oi = stable_sigmoid(z_before_oi / temperature)
        p_after_oi = stable_sigmoid((z_before_oi + oi_contribution) / temperature)
        oi_shift = p_after_oi - p_before_oi

        p_after_conc = stable_sigmoid((z_before_oi + oi_contribution + concentration_contribution) / temperature)
        concentration_shift = p_after_conc - p_after_oi

        z_before_micro = z_before_oi + oi_contribution + concentration_contribution
        p_after_micro = stable_sigmoid((z_before_micro + micro_contribution) / temperature)
        micro_shift = p_after_micro - stable_sigmoid(z_before_micro / temperature)

        decomposition = {
            "market_prior": round(market_price, 4),
            "whale_shift": round(whale_shift, 4),
            "smart_money_shift": round(smart_money_shift, 4),
            "fresh_wallet_shift": round(fresh_wallet_shift, 4),
            "cluster_shift": round(cluster_shift, 4),
            "registry_shift": round(registry_shift, 4),
            "oi_shift": round(oi_shift, 4),
            "concentration_shift": round(concentration_shift, 4),
            "micro_shift": round(micro_shift, 4),
            "swan_impact": 0.0,
            "oracle_score": round(oracle_score, 4),
            "p_raw": round(p_raw, 4),
            "top_whales": top_whales,
            "confidence": {
                "n_whales": result.get("n_whales", 0),
                "n_holders": result.get("n_holders", 0),
                "has_microstructure": micro_contribution != 0.0,
                "has_oi": oi_contribution != 0.0,
            },
        }

        decomposition["cluster_discount_summary"] = {
            "cluster_discounts_applied": result.get("cluster_discounts_applied", 0),
            "avg_cluster_discount": result.get("avg_cluster_discount", 0),
            "discounted_whales": [
                f"{a[:6]}...{a[-4:]}" if len(a) >= 10 else a for a in result.get("discounted_whale_addresses", [])
            ],
            "cluster_version": "v2",
        }

        return decomposition

    # === compute_analytics — адаптация из старого кода для UI ===

    def compute_analytics(
        self,
        oracle_score: float,
        current_price: float,
        previous_oracle_score: float | None,
        z_posterior: float | None,
        n_whales: int,
        *,
        whale_contribution: float | None = None,
    ) -> dict:
        """
        Layer 1 аналитика для UI (без LLM).

        Адаптированная версия из старого OracleScoreCalculator.

        Returns:
            Dict с divergence, confidence, trend, risk, bayesian (опционально)
        """
        oracle = oracle_score if oracle_score is not None else 0.5
        price = current_price if current_price is not None else 0.5

        # --- Divergence ---
        delta = oracle - price
        abs_delta = abs(delta)

        if delta > 0.10:
            signal = "underpriced"
        elif delta < -0.10:
            signal = "overpriced"
        else:
            signal = "fair"

        if abs_delta >= 0.15:
            magnitude = "strong"
        elif abs_delta > 0.10:
            magnitude = "moderate"
        else:
            magnitude = "weak"

        # --- Confidence (data quality) ---
        if n_whales >= 5:
            data_quality = "excellent"
        elif n_whales >= 2:
            data_quality = "fair"
        else:
            data_quality = "poor"

        # --- Trend ---
        if previous_oracle_score is not None:
            trend_delta = oracle - previous_oracle_score
            if trend_delta > 0.01:
                trend_direction = "rising"
            elif trend_delta < -0.01:
                trend_direction = "falling"
            else:
                trend_direction = "stable"
        else:
            trend_delta = 0.0
            trend_direction = "unknown"

        # --- Risk ---
        risk_level = "none"  # deprecated: p_swan removed from pipeline

        result = {
            "divergence": {
                "oracle_vs_price": round(delta, 4),
                "signal": signal,
                "magnitude": magnitude,
            },
            "confidence": {
                "data_quality": data_quality,
                "n_whales": n_whales,
            },
            "trend": {
                "direction": trend_direction,
                "delta": round(trend_delta, 4),
            },
            "risk": {
                "p_swan": 0.0,
                "risk_level": risk_level,
            },
        }

        # Bayesian breakdown для UI декомпозиции Oracle Score
        p_raw = max(EPSILON, min(1.0 - EPSILON, oracle))

        result["bayesian"] = {
            "z_posterior": round(z_posterior, 4) if z_posterior is not None else round(safe_logit(p_raw), 4),
            "p_raw": round(p_raw, 4),
            "p_swan": 0.0,
            "whale_contribution": round(whale_contribution, 4) if whale_contribution is not None else None,
            "market_prior": round(price, 4),
        }

        return result


class Calibrator:
    """
    Калибровка и оценка точности Bayesian Oracle.

    Используется после закрытия рынков (когда исходы известны):
    1. log_loss() — для оптимизации весов
    2. brier_score() — для оценки общей точности
    3. temperature_scale() — для коррекции overconfidence
    """

    def log_loss(
        self,
        predictions: list[float],
        outcomes: list[int],
        alpha: float = 0.01,
        weights: list[float] | None = None,
    ) -> float:
        """
        L2-Regularized Cross-Entropy Loss.

        §2.3 MATH_SPEC:
            L = -(1/N) * sum[y*ln(p) + (1-y)*ln(1-p)] + (alpha/2) * ||w||^2

        Predictions clamped в [eps, 1-eps] для защиты от ln(0).

        Args:
            predictions: Список P_oracle [0, 1]
            outcomes: Список фактических исходов {0, 1}
            alpha: L2 regularization strength (default 0.01)
            weights: Текущие веса модели (для L2 term). None -> без L2.

        Returns:
            Loss value (чем ниже — тем лучше). Minimum = 0.
        """
        n = len(predictions)
        if n == 0:
            return 0.0

        total = 0.0
        for p, y in zip(predictions, outcomes):
            p_safe = max(EPSILON, min(1.0 - EPSILON, p))
            total += y * math.log(p_safe) + (1 - y) * math.log(1.0 - p_safe)

        ce = -total / n

        # L2 regularization term
        l2_term = 0.0
        if weights is not None:
            l2_term = (alpha / 2.0) * sum(w * w for w in weights)

        return ce + l2_term

    def brier_score(
        self,
        predictions: list[float],
        outcomes: list[int],
    ) -> float:
        """
        Brier Score (Mean Squared Error of probabilities).

        §2.4 MATH_SPEC:
            BS = (1/N) * sum(p_i - y_i)^2

        Args:
            predictions: Список P_oracle [0, 1]
            outcomes: Список фактических исходов {0, 1}

        Returns:
            Brier Score [0, 1]. < 0.10 = отличный, > 0.25 = хуже случайного.
        """
        n = len(predictions)
        if n == 0:
            return 0.0

        return sum((p - y) ** 2 for p, y in zip(predictions, outcomes)) / n

    def brier_decomposition(
        self,
        predictions: list[float],
        outcomes: list[int],
        n_bins: int = 10,
    ) -> dict:
        """
        Murphy Decomposition: BS = Reliability - Resolution + Uncertainty.

        Reliability → 0 = идеально откалиброван.
        Resolution → выше = лучше (оракул выдаёт разнообразные ответы).
        Uncertainty = base_rate × (1 - base_rate) — константа.

        Args:
            predictions: Список P_oracle [0, 1]
            outcomes: Фактические исходы {0, 1}
            n_bins: Количество бинов для калибровочного графика

        Returns:
            Dict с brier_score, reliability, resolution, uncertainty, diagnosis
        """
        n = len(predictions)
        if n == 0:
            return {
                "brier_score": 0.0,
                "reliability": 0.0,
                "resolution": 0.0,
                "uncertainty": 0.0,
                "base_rate": 0.0,
                "n_bins": n_bins,
                "diagnosis": "no data",
                "bin_details": [],
            }

        base_rate = sum(outcomes) / n
        uncertainty = base_rate * (1 - base_rate)

        # Разбиваем по бинам
        bins: list[list[tuple[float, int]]] = [[] for _ in range(n_bins)]
        for p, y in zip(predictions, outcomes):
            idx = min(int(p * n_bins), n_bins - 1)
            bins[idx].append((p, y))

        reliability = 0.0
        resolution = 0.0
        bin_details = []

        for k, bin_items in enumerate(bins):
            if not bin_items:
                continue
            n_k = len(bin_items)
            avg_pred = sum(p for p, _ in bin_items) / n_k
            avg_outcome = sum(y for _, y in bin_items) / n_k
            reliability += n_k * (avg_pred - avg_outcome) ** 2
            resolution += n_k * (avg_outcome - base_rate) ** 2
            bin_details.append(
                {
                    "bin": k,
                    "n": n_k,
                    "avg_pred": round(avg_pred, 4),
                    "avg_outcome": round(avg_outcome, 4),
                    "gap": round(avg_pred - avg_outcome, 4),
                }
            )

        reliability /= n
        resolution /= n
        brier = reliability - resolution + uncertainty

        # Диагностика
        if reliability > resolution and resolution > 0:
            diagnosis = "signals_add_noise"
        elif resolution < uncertainty * 0.3:
            diagnosis = "oracle_too_conservative"
        else:
            diagnosis = "ok"

        return {
            "brier_score": round(brier, 6),
            "reliability": round(reliability, 6),
            "resolution": round(resolution, 6),
            "uncertainty": round(uncertainty, 6),
            "base_rate": round(base_rate, 4),
            "n_bins": n_bins,
            "diagnosis": diagnosis,
            "bin_details": bin_details,
        }

    def beta_calibrate(self, p_raw: float, a: float = 1.0, b: float = 0.0) -> float:
        """
        Beta Calibration (Kull et al., 2017): P_cal = sigmoid(a × logit(p) + b).

        2 параметра (a, b) вместо одного T в Temperature Scaling.
        a контролирует slope (>1 = sharpen, <1 = flatten).
        b контролирует bias (>0 = сдвиг к YES, <0 = к NO).

        При a=1, b=0 → identity (без изменений).

        Args:
            p_raw: Некалиброванная вероятность [0, 1]
            a: Slope parameter (default 1.0)
            b: Bias parameter (default 0.0)

        Returns:
            Калиброванная вероятность в (0, 1)
        """
        z = safe_logit(p_raw)
        return stable_sigmoid(a * z + b)

    def find_optimal_beta_params(
        self,
        z_posteriors: list[float],
        outcomes: list[int],
        a_range: tuple[float, float] = (0.5, 2.0),
        b_range: tuple[float, float] = (-1.0, 1.0),
        a_steps: int = 50,
        b_steps: int = 40,
    ) -> tuple[float, float]:
        """
        Grid search оптимальных (a, b) для Beta Calibration.

        Минимизирует Log Loss на валидационном наборе.
        """
        if not z_posteriors or not outcomes:
            return 1.0, 0.0

        best_a, best_b = 1.0, 0.0
        best_loss = float("inf")

        a_min, a_max = a_range
        b_min, b_max = b_range

        for i in range(a_steps + 1):
            a = a_min + (a_max - a_min) * i / a_steps
            for j in range(b_steps + 1):
                b = b_min + (b_max - b_min) * j / b_steps
                preds = [stable_sigmoid(a * z + b) for z in z_posteriors]
                loss = self.log_loss(preds, outcomes, alpha=0.0)
                if loss < best_loss:
                    best_loss = loss
                    best_a, best_b = a, b

        return best_a, best_b

    def temperature_scale(
        self,
        z_posterior: float,
        temperature: float = 1.0,
    ) -> float:
        """
        Temperature Scaling для коррекции overconfidence.

        T = 1.0: без изменений
        T > 1.0: "размывает" уверенность (0.90 -> 0.80)
        T < 1.0: "усиливает" уверенность (0.60 -> 0.70)
        """
        if temperature <= 0:
            temperature = 1.0
        return stable_sigmoid(z_posterior / temperature)

    def find_optimal_temperature(
        self,
        z_posteriors: list[float],
        outcomes: list[int],
        t_range: tuple[float, float] = (0.5, 5.0),
        steps: int = 100,
    ) -> float:
        """
        Найти оптимальную температуру T методом grid search.

        Минимизирует Log Loss на валидационном наборе:
            T* = argmin_T sum[-y*ln(sigma(Z/T)) - (1-y)*ln(1-sigma(Z/T))]

        Args:
            z_posteriors: Список Z_posterior для валидационных рынков
            outcomes: Фактические исходы {0, 1}
            t_range: Диапазон поиска (min_T, max_T)
            steps: Количество шагов grid search

        Returns:
            Оптимальная температура T*
        """
        if not z_posteriors or not outcomes:
            return 1.0

        best_t = 1.0
        best_loss = float("inf")

        t_min, t_max = t_range
        for i in range(steps + 1):
            t = t_min + (t_max - t_min) * i / steps
            preds = [stable_sigmoid(z / t) for z in z_posteriors]
            loss = self.log_loss(preds, outcomes, alpha=0.0)

            if loss < best_loss:
                best_loss = loss
                best_t = t

        return best_t


# === Factory functions ===


def get_bayesian_calculator(weights: dict[str, float] | None = None) -> BayesianScoreCalculator:
    """Создать Bayesian Score Calculator с параметрами из Settings."""
    from app.core.config import get_settings

    settings = get_settings()

    if weights is None:
        weights = {
            "w_mkt": settings.scoring_w_mkt,
            "w_whale": settings.scoring_w_whale,
            "w_whale_contra": settings.scoring_w_whale_contra,
            "w_fresh": settings.scoring_w_fresh,
            "w_cluster": settings.scoring_w_cluster,
            "w_registry": settings.scoring_w_registry,
            "w_oi": settings.scoring_w_oi,
            "w_concentration": settings.scoring_w_concentration,
            "w_micro": settings.scoring_w_micro,
            "w_sports_odds": settings.scoring_w_sports_odds,
        }

    return BayesianScoreCalculator(
        weights=weights,
        f_max=settings.scoring_f_max,
        k_liquidity=settings.scoring_k_liquidity,
        expert_weight_enabled=settings.scoring_expert_weight_enabled,
        timing_boost_scale=settings.scoring_timing_boost_scale,
        prior_alpha=settings.scoring_prior_alpha,
        prior_beta=settings.scoring_prior_beta,
        credibility_gamma=settings.scoring_credibility_gamma,
        decay_alpha=settings.scoring_decay_alpha,
    )


def get_oracle_calculator() -> BayesianScoreCalculator:
    """Backward-compatible алиас."""
    return get_bayesian_calculator()
