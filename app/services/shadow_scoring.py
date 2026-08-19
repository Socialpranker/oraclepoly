"""Shadow scoring: run pure scoring_pure.score() alongside legacy, log diffs.

Invoked from BayesianScoreCalculator.calculate_oracle_score() when settings.scoring_pure_enabled
is True. No state change to live pipeline output — only observability.

After 48h of zero diffs > DIFF_THRESHOLD, cutover via settings.scoring_pure_cutover.
"""

from __future__ import annotations

import structlog

from app.core.scoring_config_loader import load_prod_config
from app.core.scoring_pure import score
from app.models.feature_vector import FeatureVector

logger = structlog.get_logger()

# Task 4 equivalence tests prove pure == legacy exactly (modulo float rounding).
# Threshold 1e-6 catches real math drift — not relaxed for calibration difference.
DIFF_THRESHOLD = 1e-6


async def compare_scores_for_snapshot(
    fv: FeatureVector,
    legacy_oracle_score: float,
) -> dict:
    """Run pure score on fv, compare to legacy_oracle_score. Log + return summary.

    Async so callers can fire-and-forget via loop.create_task without blocking
    calculate_oracle_score (no internal awaits).
    """
    cfg = load_prod_config()
    pure = score(fv, cfg)

    signed_diff = pure.oracle_score - legacy_oracle_score
    diff = abs(signed_diff)
    diverges = diff > DIFF_THRESHOLD

    log_event = "shadow_divergence" if diverges else "shadow_match"
    logger.info(
        log_event,
        market_id=fv.market_id,
        legacy=round(legacy_oracle_score, 6),
        pure=round(pure.oracle_score, 6),
        diff=round(diff, 6),
        signed_diff=round(signed_diff, 6),
        config_hash=pure.config_hash,
        components=sorted(pure.components_used),
    )

    return {
        "diverges": diverges,
        "diff": diff,
        "signed_diff": signed_diff,
        "pure": pure.oracle_score,
        "legacy": legacy_oracle_score,
        "config_hash": pure.config_hash,
    }
