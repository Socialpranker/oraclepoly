"""Mandatory promote gates: N≥min + Wilson lower bounds + adversarial mandatory.

Phase 0 Stage 3. См. docs/superpowers/plans/2026-05-honest-baseline/03-mandatory-gates.md.

Public API:
    evaluate_promote_gates(...) -> dict with:
        "decision": "READY" | "INSUFFICIENT_DATA" | "BLOCKED" | "MANUAL_OVERRIDE_NEEDED"
        "gates": {gate_name: {"passed": bool, "value": Any, ...}, ...}

Decision priority (highest first):
    1. INSUFFICIENT_DATA — N_terminal < min_n. Не блокируем по другим gates,
       ждём накопления данных.
    2. MANUAL_OVERRIDE_NEEDED — N OK, но adversarial_mirror = NULL (не evaluated).
       Требуется human decision (POST /v2/lifecycle/adversarial_mirror).
    3. BLOCKED — N OK, adversarial evaluated, но что-то fail (PnL<0, low Wilson,
       <14d, exec disabled, или adversarial=False).
    4. READY — все 6 gates passed.

Используется в:
    - GET /v2/lifecycle/live_readiness (UI display)
    - TG promote callback (Stage 4 — validation перед UPDATE detector_state)
"""

from __future__ import annotations

from typing import Any

from app.services.wilson_ci import wilson_score_interval


def evaluate_promote_gates(
    *,
    n_terminal: int,
    n_placed: int,
    n_filled: int,
    n_would_have_filled: int = 0,
    wins: int,
    losses: int,
    sum_roi_net: float,
    mean_roi_net: float | None = None,
    sum_neg_sq_roi: float = 0.0,
    median_entry_price: float | None = None,
    days_virtual: int,
    adversarial_pass: bool | None,
    execution_enabled: bool,
    min_n: int | None = None,
    wilson_confidence: float | None = None,
    fill_threshold: float | None = None,
    winrate_threshold: float | None = None,
) -> dict[str, Any]:
    """Pure-logic evaluation of all 6 promote gates.

    v4 2026-05-29: PnL-гейт (G1) судит по SIGNAL-LEVEL realized_roi_net
    (sum_roi_net + Sortino) — той же честной экономике, что promote shadow→virtual
    (compute_promotion_gates) и demote. НЕ по order-level pnl_usd, который надут
    paper EARLY_EXIT (STANDALONE +$12K paper при resolved −$717). Робастно
    (signal-level N=6292 vs order-level 4). См. memory [[shadow-roi-virtual-pnl]].

    Args:
        n_terminal: signal-level terminal (WIN+LOSS resolved signals).
        n_placed: virtual orders placed (включая filled, placed, cancelled).
        n_filled: virtual orders filled (subset of n_placed).
        n_would_have_filled: rejected из-за price drift (would-have-filled,
            2b semantics — считаются filled для fill rate). default 0.
        wins: SIGNAL-level wins (для Wilson win-rate floor).
        losses: SIGNAL-level losses.
        sum_roi_net: Σ realized_roi_net (%) по terminal signals (G1 economics).
        mean_roi_net: средний realized_roi_net (%) — для Sortino.
        sum_neg_sq_roi: Σ min(R,0)² — downside semivariance numerator для Sortino.
        days_virtual: дней с promoted_at до сейчас.
        adversarial_pass: True / False / None. None blocks (MANUAL_OVERRIDE_NEEDED).
        execution_enabled: settings.execution_enabled flag.
        min_n: override default из settings.promote_gate_min_n_terminal (=30).
        wilson_confidence: override default (=0.95).
        fill_threshold: override default (=0.50).
        winrate_threshold: override default (=0.50).

    Returns:
        {"decision": str, "gates": dict}
    """
    from app.services.detector_metrics import _SORTINO_THRESHOLD, compute_sortino

    # Settings defaults
    if min_n is None or wilson_confidence is None or fill_threshold is None or winrate_threshold is None:
        from app.core.config import get_settings

        s = get_settings()
        if min_n is None:
            min_n = s.promote_gate_min_n_terminal
        if wilson_confidence is None:
            wilson_confidence = s.promote_gate_wilson_confidence
        if fill_threshold is None:
            fill_threshold = s.promote_gate_wilson_threshold_fill
        if winrate_threshold is None:
            winrate_threshold = s.promote_gate_wilson_threshold_winrate

    # ── G0: N min ────────────────────────────────────────────────────────────
    g_n_min = {
        "passed": n_terminal >= min_n,
        "value": n_terminal,
        "threshold": min_n,
        "label": f"N_terminal ≥ {min_n}",
    }

    # ── G2: Wilson fill rate ─────────────────────────────────────────────────
    # would-have-filled (price-drift rejections) переклассифицируются как filled:
    # цена ушла В НАШУ сторону = edge был, опоздали. См. effective_fill_counts.
    from app.services.detector_metrics import effective_fill_counts

    n_filled_eff, n_placed_eff = effective_fill_counts(n_filled, n_would_have_filled, n_placed)
    if n_placed_eff > 0:
        fill_lower, fill_upper = wilson_score_interval(n_filled_eff, n_placed_eff, wilson_confidence)
        fill_rate_pct = round(n_filled_eff / n_placed_eff * 100, 1)
        g_fill = {
            "passed": fill_lower >= fill_threshold,
            "value": fill_rate_pct,
            "wilson_lower_pct": round(fill_lower * 100, 1),
            "wilson_upper_pct": round(fill_upper * 100, 1),
            "threshold_pct": round(fill_threshold * 100, 1),
            "n_would_have_filled": n_would_have_filled,
            "label": f"Wilson lower ≥ {fill_threshold * 100:.0f}% (incl would-have-filled)",
        }
    else:
        # Нет placed → нет данных. Не блокируем (нет evidence against).
        g_fill = {
            "passed": True,
            "value": None,
            "wilson_lower_pct": None,
            "wilson_upper_pct": None,
            "threshold_pct": round(fill_threshold * 100, 1),
            "label": "no placed orders (skipped)",
        }

    # ── G1: signal-level economics (sum_roi_net>0 AND Sortino>1.0) AND Wilson WR ─
    # v4: судим по честной hold-to-resolution экономике + downside-aware Sortino,
    # НЕ по order-level pnl_usd (paper-надутый). Симметрично promote shadow→virtual.
    terminal = wins + losses
    if terminal > 0:
        wr_lower, wr_upper = wilson_score_interval(wins, terminal, wilson_confidence)
        wr_pct = round(wins / terminal * 100, 1)
    else:
        wr_lower, wr_upper = 0.0, 1.0
        wr_pct = None

    # Payout-aware break-even WR floor — симметрично compute_promotion_gates G4.
    # Code-review fix #532: flat 50% floor math-блокировал прибыльные NO-fade/underdog
    # детекторы (hold-to-resolution WR структурно ~5-25% при payout 7-20×). BE WR =
    # 1/(1+payout), payout=1/median_entry. Fallback на winrate_threshold если entry нет.
    be_wr_frac = winrate_threshold
    be_wr_source = "fallback_flat"
    if median_entry_price is not None and 0 < float(median_entry_price) < 1:
        payout = 1.0 / float(median_entry_price)
        be_wr_frac = 1.0 / (1.0 + payout)
        be_wr_source = "payout_aware"

    sortino = compute_sortino(mean_roi_net, sum_neg_sq_roi, terminal)
    sortino_pass = sortino is not None and sortino > _SORTINO_THRESHOLD
    pnl_pass = sum_roi_net > 0 and sortino_pass and wr_lower >= be_wr_frac
    g_pnl = {
        "passed": pnl_pass,
        "value": round(sum_roi_net, 1),  # signal ROI % (было order pnl_usd)
        "sum_roi_net_pct": round(sum_roi_net, 1),
        "sortino": sortino if sortino != float("inf") else "inf",
        "sortino_threshold": _SORTINO_THRESHOLD,
        "winrate_pct": wr_pct,
        "wilson_lower_winrate_pct": round(wr_lower * 100, 1) if terminal > 0 else None,
        "wilson_upper_winrate_pct": round(wr_upper * 100, 1) if terminal > 0 else None,
        "break_even_wr_pct": round(be_wr_frac * 100, 1),
        "break_even_wr_source": be_wr_source,
        "label": (
            f"signal ROI > 0 AND Sortino > {_SORTINO_THRESHOLD} "
            f"AND Wilson win-rate lower ≥ {be_wr_frac * 100:.1f}% ({be_wr_source})"
        ),
    }

    # ── G3: Adversarial mirror (mandatory) ───────────────────────────────────
    if adversarial_pass is None:
        g_adv = {
            "passed": False,
            "value": "not_evaluated",
            "label": "Adversarial mirror evaluated (manual)",
        }
    elif adversarial_pass is True:
        g_adv = {
            "passed": True,
            "value": "pass",
            "label": "Adversarial mirror evaluated (manual)",
        }
    else:
        g_adv = {
            "passed": False,
            "value": "fail",
            "label": "Adversarial mirror evaluated (manual)",
        }

    # ── G4: Days accumulated ─────────────────────────────────────────────────
    g_days = {
        "passed": days_virtual >= 14,
        "value": days_virtual,
        "threshold": 14,
        "label": "≥14d virtual data",
    }

    # ── G5: Execution enabled ────────────────────────────────────────────────
    g_exec = {
        "passed": execution_enabled,
        "value": execution_enabled,
        "label": "EXECUTION_ENABLED=true",
    }

    gates = {
        "n_min": g_n_min,
        "pnl_positive": g_pnl,
        "fill_rate": g_fill,
        "adversarial_mirror": g_adv,
        "days_accumulated": g_days,
        "execution_enabled": g_exec,
    }

    # ── Decision priority ────────────────────────────────────────────────────
    # 1. N gate fail dominates (нет данных → невозможно решить про edge)
    if not g_n_min["passed"]:
        decision = "INSUFFICIENT_DATA"
    # 2. Adversarial not_evaluated требует human override
    elif g_adv["value"] == "not_evaluated":
        decision = "MANUAL_OVERRIDE_NEEDED"
    # 3. Все gates pass → READY
    elif all(g["passed"] for g in (g_pnl, g_fill, g_adv, g_days, g_exec)):
        decision = "READY"
    # 4. Иначе — есть activle failure → BLOCKED
    else:
        decision = "BLOCKED"

    return {"decision": decision, "gates": gates}
