"""ClusterClassifier — lookup table на основе кластерного анализа 220K snapshots.

Каждый snapshot классифицируется в tier (T1/T2/T3) с expected_accuracy.
Данные: research/oracle-edge-cluster-analysis/findings_v3_corrected.md

Пороги: whale_type abs(raw) < 0.1 (weak), no_wh = tx==0 AND raw==0.
Useful range: oracle_score 10-90%. Negative hours_to_resolve excluded.

Для fade-сигналов (whale agree → Tier 3): expected_accuracy = 1 - original,
т.к. стратегия ставит ПРОТИВ китов.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum


class Tier(IntEnum):
    T1 = 1  # MAX BET: 80%+ accuracy, verified stable
    T2 = 2  # BIG BET: 70-80% accuracy
    T3 = 3  # AVOID / FADE: <55% accuracy


@dataclass(frozen=True, slots=True)
class ClusterResult:
    tier: Tier
    expected_accuracy: float  # для fade = inverted (1 - original)
    is_fade_signal: bool
    cluster_label: str


def _parse_dt(val) -> datetime | None:
    if not val:
        return None
    try:
        s = str(val).replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _resolve_segment(created_at, end_date) -> str:
    """Время до resolve: <24h, 1-3d, 3-7d, >7d, unknown."""
    dt_created = _parse_dt(created_at)
    dt_end = _parse_dt(end_date)
    if not dt_created or not dt_end:
        return "unknown"
    hours = (dt_end - dt_created).total_seconds() / 3600
    if hours < 0:
        return "unknown"
    if hours < 24:
        return "<24h"
    if hours < 72:
        return "1-3d"
    if hours < 168:
        return "3-7d"
    return ">7d"


def _whale_type(whale_raw: float, whale_tx: int) -> str:
    if whale_tx == 0 and whale_raw == 0:
        return "no_wh"
    if abs(whale_raw) < 0.1:
        return "weak"
    if whale_raw < 0:
        return "contra"
    return "agree"


def _has_registry(registry_raw: float) -> bool:
    return abs(registry_raw) > 0


# ─── Lookup table v3 (corrected 2026-03-25) ──────────────────────────────────
# Верифицировано на 220,922 useful-range snapshots с production thresholds.
# (category, segment, whale_type, has_registry) → (tier, accuracy)
# accuracy = оригинальная (для fade signals инвертируется ниже)

_CLUSTER_TABLE: dict[tuple[str, str, str, bool], tuple[Tier, float]] = {
    # ── TIER 1: 80%+ verified accuracy ──
    # Tier 1A — Narrow (N=1,436, pool_acc=92.7%, 192 markets)
    ("politics", "1-3d", "agree", True): (Tier.T1, 1.000),  # N=41
    ("politics", "1-3d", "contra", False): (Tier.T1, 1.000),  # N=33
    ("politics", ">7d", "contra", False): (Tier.T1, 1.000),  # N=81
    ("sports", ">7d", "contra", True): (Tier.T1, 1.000),  # N=29
    ("tech & ai", "1-3d", "weak", True): (Tier.T1, 1.000),  # N=54
    ("tech & ai", ">7d", "weak", False): (Tier.T1, 0.976),  # N=41
    ("crypto", "1-3d", "weak", True): (Tier.T1, 0.964),  # N=56
    ("crypto", ">7d", "weak", True): (Tier.T1, 0.960),  # N=99
    ("culture", "<24h", "weak", False): (Tier.T1, 0.951),  # N=41
    ("sports", ">7d", "contra", False): (Tier.T1, 0.917),  # N=48
    ("tech & ai", ">7d", "no_wh", False): (Tier.T1, 0.903),  # N=507
    ("crypto", "3-7d", "weak", True): (Tier.T1, 0.901),  # N=101
    ("politics", "3-7d", "weak", False): (Tier.T1, 0.895),  # N=276
    ("politics", "3-7d", "contra", True): (Tier.T1, 0.800),  # N=20
    ("politics", "3-7d", "contra", False): (Tier.T1, 1.000),  # N=9
    # Tier 1B — Bulk (N=17,729, acc=80.7%, 1,781 markets)
    ("sports", ">7d", "no_wh", False): (Tier.T1, 0.807),  # N=17729
    # ── TIER 2: 70-80% verified accuracy ──
    ("politics", "1-3d", "weak", False): (Tier.T2, 0.891),  # N=110
    ("culture", "<24h", "agree", True): (Tier.T2, 0.913),  # N=23, tiny
    ("culture", "<24h", "agree", False): (Tier.T2, 0.846),  # N=91
    ("culture", "1-3d", "agree", False): (Tier.T2, 0.806),  # N=98
    ("culture", ">7d", "no_wh", False): (Tier.T2, 0.806),  # N=594
    ("politics", "3-7d", "agree", False): (Tier.T2, 0.838),  # N=278
    ("tech & ai", "3-7d", "agree", False): (Tier.T2, 0.833),  # N=168
    ("crypto", "3-7d", "agree", True): (Tier.T2, 0.802),  # N=86
    ("crypto", ">7d", "contra", True): (Tier.T2, 0.840),  # N=163
    ("crypto", "1-3d", "agree", True): (Tier.T2, 0.794),  # N=180
    ("crypto", "<24h", "contra", False): (Tier.T2, 0.795),  # N=122
    ("crypto", "<24h", "no_wh", False): (Tier.T2, 0.705),  # N=7857
    ("sports", "3-7d", "no_wh", False): (Tier.T2, 0.643),  # N=17664
    ("politics", "<24h", "contra", False): (Tier.T2, 0.857),  # N=49
    # ── TIER 3: AVOID (<55% accuracy) ──
    ("world & economy", "3-7d", "weak", False): (Tier.T3, 0.000),  # N=54
    ("culture", "3-7d", "agree", False): (Tier.T3, 0.367),  # N=60
    ("sports", "3-7d", "contra", True): (Tier.T3, 0.407),  # N=150
    ("sports", "1-3d", "weak", True): (Tier.T3, 0.461),  # N=369
    ("culture", "<24h", "no_wh", False): (Tier.T3, 0.490),  # N=924
    ("politics", "<24h", "agree", False): (Tier.T3, 0.500),  # N=68
    ("crypto", "1-3d", "weak", False): (Tier.T3, 0.538),  # N=390
    ("sports", "3-7d", "weak", True): (Tier.T3, 0.549),  # N=512
    ("politics", ">7d", "weak", False): (Tier.T3, 0.550),  # N=171
}

_CATEGORY_DEFAULTS: dict[str, float] = {
    "crypto": 0.70,
    "sports": 0.67,
    "politics": 0.72,
    "tech & ai": 0.73,
    "culture": 0.70,
    "world & economy": 0.60,
}

_DEFAULT_ACCURACY = 0.67


def classify_snapshot(snap: dict) -> ClusterResult:
    """Классифицировать snapshot в tier с expected accuracy.

    Поддерживает оба ключа: created_at (raw snapshots) и snapshot_created_at (compute_live_signals output).
    Для fade signals (whale agree): expected_accuracy инвертируется (1 - original).
    """
    category = (snap.get("category") or "").lower().strip()
    created = snap.get("created_at") or snap.get("snapshot_created_at")
    segment = _resolve_segment(created, snap.get("end_date"))

    whale_raw = float(snap.get("whale_contribution_raw") or 0)
    whale_tx = int(snap.get("whale_tx_count") or 0)
    registry_raw = float(snap.get("sm_registry_raw") or 0)

    wtype = _whale_type(whale_raw, whale_tx)
    has_reg = _has_registry(registry_raw)

    key = (category, segment, wtype, has_reg)
    label = f"{category}:{segment}:{wtype}:{'reg+' if has_reg else 'reg-'}"

    if key in _CLUSTER_TABLE:
        tier, accuracy = _CLUSTER_TABLE[key]
        is_fade = wtype == "agree" and tier == Tier.T3
        fade_accuracy = 1.0 - accuracy if is_fade else accuracy
        return ClusterResult(
            tier=tier,
            expected_accuracy=fade_accuracy,
            is_fade_signal=is_fade,
            cluster_label=label,
        )

    # Fallback: conservative — без явных данных не рискуем
    default_acc = _CATEGORY_DEFAULTS.get(category, _DEFAULT_ACCURACY)

    if wtype == "weak" and has_reg:
        # Паттерн weak+reg+ стабильно хорош
        return ClusterResult(
            tier=Tier.T2,
            expected_accuracy=min(default_acc + 0.08, 0.85),
            is_fade_signal=False,
            cluster_label=f"{label}:fallback",
        )
    if wtype == "contra" and segment in (">7d", "3-7d"):
        # contra + long resolve = надёжный сигнал
        return ClusterResult(
            tier=Tier.T2,
            expected_accuracy=min(default_acc + 0.05, 0.85),
            is_fade_signal=False,
            cluster_label=f"{label}:fallback",
        )
    return ClusterResult(
        tier=Tier.T2 if default_acc >= 0.70 else Tier.T3,
        expected_accuracy=default_acc,
        is_fade_signal=False,
        cluster_label=f"{label}:fallback",
    )
