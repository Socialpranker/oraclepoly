"""D45: Chainlink lead-lag detector.

Гипотеза: Pyth Hermes публикует свежую crypto-цену с ~440ms cadence (proxy
для Chainlink Data Streams). Polymarket CLOB midpoint реагирует медленнее
(3-10s). В окне резолюции crypto-updown markets last 5-30s до end_date это
даёт **lead-lag edge**: мы знаем, что Chainlink уже двинулся, а CLOB ещё нет.

Architecture (Shadow only — никогда execution):
    cron (Stage 2) → fetch active updown markets → PythStreamService snapshot
                  → detect() → emit Signal в detector_signals (resolved=false)
                  → resolve job через ~5 минут пишет outcome + realized_roi

Логика detect():
    1. Pre-condition: now в окне [window_end - max_sec, window_end - min_sec]
    2. pyth_move_pct = (price_now - price_at_window_open) / price_at_window_open × 100
    3. clob_implied_move_pct = (yes_midpoint - 0.5) × 200
       (грубая аппроксимация: CLOB 50% = no expectation,
        100% = "наверняка UP", 0% = "наверняка DOWN")
    4. divergence = pyth_move_pct - clob_implied_move_pct
    5. Если |divergence| < threshold → None
    6. side = "YES" если divergence > 0 (Pyth говорит UP, CLOB не отразил),
       "NO" если divergence < 0
    7. confidence = min(|divergence| / 5.0, 0.95)

Important caveat: Pyth ≠ Chainlink. Polymarket резолвит **по Chainlink CEXprice**
(aggregate Binance+Coinbase+Kraken+Bitstamp). Pyth basis может фальшиво
сигналить если Pyth опережает Chainlink. См. Stage 4 backtest для empirical
валидации basis distribution. Threshold 0.3% выбран консервативно с buffer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from app.detectors.contract import Signal
from app.detectors.token_resolver import resolve_token_id

DETECTOR_NAME = "d45_chainlink_leadlag"

# Defaults — настраиваются caller'ом
DEFAULT_MIN_DIVERGENCE_PCT = 0.3
DEFAULT_MIN_SECONDS_TO_RESOLUTION = 5
DEFAULT_MAX_SECONDS_TO_RESOLUTION = 30
DEFAULT_CONFIDENCE_SCALE_PCT = 5.0  # divergence ≥ 5pp = confidence cap 0.95
DEFAULT_CONFIDENCE_CAP = 0.95
DEFAULT_MAX_PYTH_BINANCE_BASIS_PCT = 0.5  # |pyth - binance| > 0.5% → Pyth glitch suspect


@dataclass(frozen=True)
class MarketState:
    """Snapshot Polymarket crypto-updown market в момент scan."""

    market_id: str
    condition_id: str
    slug: str  # "btc-updown-5m-1779354900"
    yes_midpoint: float  # current_price (YES side, 0..1)
    window_start_ts: int  # unix sec, parsed from slug
    window_end_ts: int  # window_start + horizon_sec
    horizon_sec: int  # 300 / 900 / 14400
    event_id: str | None = None
    token_id: str | None = None  # YES token (legacy; to_signal резолвит по side из clob_token_ids)
    clob_token_ids: list[str] | None = None  # raw [yes_token, no_token] для side-aware резолва


@dataclass(frozen=True)
class PythSnapshot:
    """Pyth state для одного symbol в момент scan."""

    symbol: str  # "BTC/USD"
    price_now: float
    price_at_window_open: float | None  # цена на window_start_ts (из Pyth REST history)
    publish_time_ms: int  # publish_time из последнего Pyth tick


@dataclass(frozen=True)
class D45Detection:
    """Внутренний результат detect — pre-Signal представление."""

    side: Literal["YES", "NO"]
    confidence: float
    pyth_move_pct: float  # signed Δ% от open
    clob_implied_move_pct: float  # signed Δ% от 0.5 baseline
    divergence_pct: float  # pyth - clob_implied
    seconds_to_resolution: int


def detect(
    market: MarketState,
    pyth: PythSnapshot,
    *,
    now_ts: int,
    binance_price_now: float | None = None,
    min_divergence_pct: float = DEFAULT_MIN_DIVERGENCE_PCT,
    min_seconds_to_resolution: int = DEFAULT_MIN_SECONDS_TO_RESOLUTION,
    max_seconds_to_resolution: int = DEFAULT_MAX_SECONDS_TO_RESOLUTION,
    confidence_scale_pct: float = DEFAULT_CONFIDENCE_SCALE_PCT,
    confidence_cap: float = DEFAULT_CONFIDENCE_CAP,
    max_pyth_binance_basis_pct: float = DEFAULT_MAX_PYTH_BINANCE_BASIS_PCT,
) -> D45Detection | None:
    """Pure detector: проверяет lead-lag divergence.

    Returns None если:
        - now вне окна [end - max_sec, end - min_sec]
        - pyth.price_at_window_open недоступен (нет history)
        - yes_midpoint вне (0, 1) — рынок невалиден
        - |divergence| < min_divergence_pct (нет signal)
        - binance_price_now передан и |pyth - binance| / binance > max_basis_pct
          (Pyth single-source glitch suspect)

    Args:
        now_ts: unix timestamp в секундах (UTC). Передаём явно для
            детерминизма тестов и idempotent scan'а.
        binance_price_now: optional independent cross-check (Binance trade).
            Если |pyth - binance| > threshold → Pyth скорее всего сломан,
            возвращаем None (защита от single-source ошибок).
    """
    # 1. Pre-condition: окно резолюции
    seconds_left = market.window_end_ts - now_ts
    if seconds_left < min_seconds_to_resolution or seconds_left > max_seconds_to_resolution:
        return None

    # 2. Sanity checks
    if pyth.price_at_window_open is None or pyth.price_at_window_open <= 0:
        return None
    if pyth.price_now <= 0:
        return None
    if not (0.0 < market.yes_midpoint < 1.0):
        return None

    # 2b. Cross-source sanity: Pyth vs Binance basis (если Binance available).
    # Большая разница = Pyth glitch (paused publisher, stale slot) → skip signal.
    if binance_price_now is not None and binance_price_now > 0:
        basis_pct = abs(pyth.price_now - binance_price_now) / binance_price_now * 100.0
        if basis_pct > max_pyth_binance_basis_pct:
            return None

    # 3. Compute moves
    pyth_move_pct = (pyth.price_now / pyth.price_at_window_open - 1.0) * 100.0
    clob_implied_move_pct = (market.yes_midpoint - 0.5) * 200.0
    divergence_pct = pyth_move_pct - clob_implied_move_pct

    # 4. Threshold gate
    if abs(divergence_pct) < min_divergence_pct:
        return None

    # 5. Side + confidence
    side: Literal["YES", "NO"] = "YES" if divergence_pct > 0 else "NO"
    confidence = min(abs(divergence_pct) / confidence_scale_pct, confidence_cap)

    return D45Detection(
        side=side,
        confidence=confidence,
        pyth_move_pct=pyth_move_pct,
        clob_implied_move_pct=clob_implied_move_pct,
        divergence_pct=divergence_pct,
        seconds_to_resolution=seconds_left,
    )


def to_signal(
    detection: D45Detection,
    market: MarketState,
    pyth: PythSnapshot,
    *,
    generated_at: datetime | None = None,
    pyth_publish_lag_ms: int | None = None,
) -> Signal:
    """Convert D45Detection в общий Signal contract.

    fair_price выводится как proxy реальной вероятности UP на основе Pyth
    движения: clamp(0.5 + pyth_move_pct/40, 0.05, 0.95). Это грубая
    аппроксимация — ML calibrator переучит на realized outcomes.
    """
    if generated_at is None:
        generated_at = datetime.now(UTC)

    fair_price_raw = 0.5 + detection.pyth_move_pct / 40.0
    fair_price = max(0.05, min(0.95, fair_price_raw))

    # Side-aware token: NO-сигнал должен нести NO-токен (index 1), не YES.
    # Раньше scan хардкодил YES → инверсия позиции в live (как был баг D3).
    # Fallback на market.token_id (legacy YES) только если clob_token_ids нет.
    token_id = resolve_token_id(market.clob_token_ids, detection.side) or market.token_id

    evidence: dict = {
        "pyth_move_pct": round(detection.pyth_move_pct, 4),
        "clob_implied_move_pct": round(detection.clob_implied_move_pct, 4),
        "divergence_pct": round(detection.divergence_pct, 4),
        "pyth_price_now": pyth.price_now,
        "pyth_price_at_window_open": pyth.price_at_window_open,
        "pyth_symbol": pyth.symbol,
        "seconds_to_resolution": detection.seconds_to_resolution,
        "window_start_ts": market.window_start_ts,
        "window_end_ts": market.window_end_ts,
        "horizon_sec": market.horizon_sec,
    }
    if pyth_publish_lag_ms is not None:
        evidence["pyth_publish_lag_ms"] = pyth_publish_lag_ms

    return Signal(
        detector=DETECTOR_NAME,
        market_id=market.market_id,
        side=detection.side,
        generated_at=generated_at,
        confidence=detection.confidence,
        expected_edge_pct=abs(detection.divergence_pct),
        fair_price=fair_price,
        current_price=market.yes_midpoint,
        horizon_sec=detection.seconds_to_resolution,
        event_id=market.event_id,
        token_id=token_id,
        evidence=evidence,
    )


# --- Slug parsing utilities (для caller — Stage 2 cron) ---


def parse_slug(slug: str) -> tuple[str, int, int] | None:
    """Извлечь (coin, window_seconds, window_start_unix_ts) из slug.

    Args:
        slug: e.g. "btc-updown-5m-1779354900"

    Returns:
        ("btc", 300, 1779354900) или None если slug не parseable.
    """
    if not slug:
        return None
    normalized = slug.lower()
    if "-updown-" not in normalized:
        return None
    parts = normalized.split("-")
    # Pattern: <coin>-updown-<horizon>-<ts>
    if len(parts) < 4:
        return None
    if parts[1] != "updown":
        return None
    coin = parts[0]
    horizon_raw = parts[2]
    try:
        ts = int(parts[3])
    except ValueError:
        return None

    window_seconds = _parse_horizon(horizon_raw)
    if window_seconds is None:
        return None
    return coin, window_seconds, ts


def _parse_horizon(raw: str) -> int | None:
    """Convert '5m' / '15m' / '4h' / '1d' в секунды."""
    if not raw:
        return None
    try:
        num = int(raw[:-1])
        unit = raw[-1]
    except (ValueError, IndexError):
        return None
    if unit == "m":
        return num * 60
    if unit == "h":
        return num * 3600
    if unit == "d":
        return num * 86400
    return None
