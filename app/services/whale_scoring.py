"""Shared scoring functions для whale copy и oracle simulation pipelines."""

from __future__ import annotations

from collections import defaultdict


def bayesian_win_rate(
    wins: int,
    losses: int,
    prior_alpha: float = 2.0,
    prior_beta: float = 2.0,
) -> float:
    """Beta posterior mean: (wins + α) / (total + α + β).

    Default prior α=β=2 → uninformative, shrinks к 50%.
    При малых выборках сильно сдвигает к prior mean.
    """
    return (wins + prior_alpha) / (wins + losses + prior_alpha + prior_beta)


def is_whale_qualified_in_category(
    cat_wins: int,
    cat_losses: int,
    min_trades: int = 5,
    min_adjusted_wr: float = 0.55,
    prior_alpha: float = 2.0,
    prior_beta: float = 2.0,
) -> tuple[bool, float]:
    """Проверить, квалифицирован ли кит для копирования в данной категории.

    Returns: (qualified, adjusted_wr). adjusted_wr=0.0 если недостаточно trades.
    """
    total = cat_wins + cat_losses
    if total < min_trades:
        return False, 0.0
    adj_wr = bayesian_win_rate(cat_wins, cat_losses, prior_alpha, prior_beta)
    return adj_wr >= min_adjusted_wr, adj_wr


def build_confluence_map(
    whale_txs: list[dict],
) -> dict[tuple[str, str], int]:
    """Подсчитать кол-во уникальных китов на (market_id, side).

    Returns: {(market_id, "YES"|"NO"): count_unique_whales}
    """
    groups: dict[tuple[str, str], set[str]] = defaultdict(set)
    for tx in whale_txs:
        key = (str(tx["market_id"]), tx["tx_outcome"].upper())
        groups[key].add(str(tx["whale_id"]))
    return {k: len(v) for k, v in groups.items()}


def calibrated_kelly(
    f_star: float,
    quality_score: float | None,
    pipeline: str = "oracle",
) -> float:
    """Unified calibrated Kelly fraction для обоих pipeline.

    Args:
        f_star: raw Kelly fraction
        quality_score: нормализованная метрика качества:
            - Oracle Sim: rolling Brier Score [0, ~0.5] (lower = better)
            - Whale Copy: 1.0 - adjusted_wr [0.3, 0.6] (lower = better)
        pipeline: "oracle" или "whale_copy" — выбирает таблицу порогов

    Returns: shrunk Kelly fraction (f_star * k)

    Oracle thresholds (Brier-based):
        ≤ 0.10 → k=0.50 | 0.10-0.15 → k=0.33 | 0.15-0.20 → k=0.25 | > 0.20 → k=0.10

    Whale thresholds (inverted WR):
        ≤ 0.35 → k=0.50 | 0.35-0.40 → k=0.33 | 0.40-0.45 → k=0.25 | 0.45-0.50 → k=0.10 | > 0.50 → k=0.00
    """
    if quality_score is None:
        k = 0.15  # no data → conservative
    elif pipeline == "oracle":
        if quality_score > 0.20:
            k = 0.10
        elif quality_score > 0.15:
            k = 0.25
        elif quality_score > 0.10:
            k = 0.33
        else:
            k = 0.50
    elif pipeline == "whale_copy":
        if quality_score > 0.50:
            k = 0.00
        elif quality_score > 0.45:
            k = 0.10
        elif quality_score > 0.40:
            k = 0.25
        elif quality_score > 0.35:
            k = 0.33
        else:
            k = 0.50
    else:
        raise ValueError(f"Unknown pipeline: {pipeline!r}. Expected 'oracle' or 'whale_copy'.")
    return f_star * k
