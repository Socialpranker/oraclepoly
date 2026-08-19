"""Wilson score interval — confidence interval для proportions устойчивая к малым N.

Стандартное решение в literature вместо Wald (который ломается на N<30 и p̂ near 0/1).
См. research/adverse-selection-queue-position-2026-05/report.md §3.

Используется как:
- promote gate в app/api/bot.py (fill rate, win rate) — Phase 0 Stage 3
- detector audit query — Phase 0 Stage 6
- anomaly detection в risk daemon — Phase 1 Stage 5

Только stdlib math: избегаем тяжёлой зависимости от scipy для одного helper.
erfinv approximation — Winitzki 2008, accuracy ~2e-4 на нашем диапазоне.
"""

from __future__ import annotations

import math


def _inv_erf(x: float) -> float:
    """Inverse error function approximation (Winitzki 2008).

    Accuracy: <0.0002 для |x|<0.99. Достаточно для 95-99% confidence intervals.

    Args:
        x: input в (-1, 1).

    Returns:
        Approximate erf^{-1}(x).

    Raises:
        ValueError если x вне (-1, 1).
    """
    if not -1.0 < x < 1.0:
        raise ValueError(f"erfinv requires x in (-1, 1), got {x}")
    if x == 0.0:
        return 0.0
    a = 0.147
    ln_term = math.log(1.0 - x * x)
    first = 2.0 / (math.pi * a) + ln_term / 2.0
    return math.copysign(1.0, x) * math.sqrt(math.sqrt(first * first - ln_term / a) - first)


def _z_score(confidence: float) -> float:
    """Inverse normal CDF для two-sided confidence interval.

    z = Phi^{-1}(1 - alpha/2), где alpha = 1 - confidence.
    Реализовано через erf: z = sqrt(2) * erfinv(2p - 1), p = 1 - alpha/2.

    Examples:
        _z_score(0.95) ≈ 1.96
        _z_score(0.99) ≈ 2.576
        _z_score(0.90) ≈ 1.645
    """
    if not 0.0 < confidence < 1.0:
        raise ValueError(f"confidence must be in (0, 1), got {confidence}")
    alpha = 1.0 - confidence
    p = 1.0 - alpha / 2.0
    return math.sqrt(2.0) * _inv_erf(2.0 * p - 1.0)


def wilson_score_interval(
    successes: int,
    total: int,
    confidence: float = 0.95,
) -> tuple[float, float]:
    """Wilson score 100*(1-α)% CI для p = successes/total.

    Преимущества над Wald:
    - При p̂=0 или p̂=1 возвращает содержательный CI (Wald даёт нулевую ширину).
    - Coverage близок к nominal даже при малых N.
    - Никогда не выходит за [0, 1].

    Args:
        successes: число успехов, 0 <= successes <= total.
        total: общее число испытаний, >= 0.
        confidence: уровень доверия в (0, 1), default 0.95.

    Returns:
        (lower, upper), оба в [0, 1].

    Edge case:
        total=0 → (0.0, 1.0). Нет информации.

    Raises:
        ValueError при невалидных входах.
    """
    if total < 0:
        raise ValueError(f"total must be >= 0, got {total}")
    if successes < 0 or successes > total:
        raise ValueError(f"successes ({successes}) must be in [0, total={total}]")
    if not 0.0 < confidence < 1.0:
        raise ValueError(f"confidence in (0, 1), got {confidence}")

    if total == 0:
        return 0.0, 1.0

    z = _z_score(confidence)
    n = float(total)
    p_hat = successes / n

    denom = 1.0 + z * z / n
    center = (p_hat + z * z / (2.0 * n)) / denom
    margin = (z / denom) * math.sqrt(p_hat * (1.0 - p_hat) / n + z * z / (4.0 * n * n))

    lower = max(0.0, center - margin)
    upper = min(1.0, center + margin)
    return lower, upper


def min_n_for_wilson_lower_bound(
    p_hat: float,
    lower_threshold: float,
    confidence: float = 0.95,
    max_search: int = 100_000,
) -> int:
    """Минимальный N такой что Wilson lower > threshold при заданном наблюдаемом p̂.

    Используется для planning: "сколько fills мне нужно accumulate чтобы убедиться
    fill_rate ≥ 50%, если current observation = 70%?"

    Реализация через iterative search (closed-form существует но iterative проще
    читать и достаточно быстр: ≤ единицы вызовов в день в Stage 6 audit).

    Args:
        p_hat: наблюдаемая proportion в [0, 1].
        lower_threshold: целевая нижняя граница в (0, 1).
        confidence: уровень доверия, default 0.95.
        max_search: верхняя граница iteration, default 100_000.

    Returns:
        Минимальный N такой что wilson_lower(round(p_hat * N), N) > lower_threshold.

    Raises:
        ValueError если p_hat <= lower_threshold (недостижимо никаким N).
    """
    if not 0.0 <= p_hat <= 1.0:
        raise ValueError(f"p_hat must be in [0, 1], got {p_hat}")
    if not 0.0 < lower_threshold < 1.0:
        raise ValueError(f"lower_threshold in (0, 1), got {lower_threshold}")
    if p_hat <= lower_threshold:
        raise ValueError(f"p_hat ({p_hat}) <= threshold ({lower_threshold}): unreachable by any N")

    for n in range(1, max_search + 1):
        successes = round(p_hat * n)
        successes = max(0, min(n, successes))
        lower, _ = wilson_score_interval(successes, n, confidence)
        if lower > lower_threshold:
            return n

    raise ValueError(f"Did not converge within max_search={max_search}")
