"""
Whale Analytics Engine

Calculates calibrated whale metrics (PnL, ROI, Timing Score, Win/Loss)
from raw Polymarket Data API data.

Output -- input data for BayesianScoreCalculator (Step 3):
- W, L -> for Laplace Smoothing: C = (W+1)/(W+L+2)
- PnL, ROI -> for ranking and filtering
- Timing Score -> additional trader quality signal
"""

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import structlog

from app.core.whale_tiers import get_whale_tier
from app.services.database import execute, fetch, fetchrow, fetchval, records_to_list

logger = structlog.get_logger()

# Conservative bankroll estimate for unknown whale.
# Better to OVERestimate B_est to not inflate Kelly fraction:
#   dp/dB ~ -1/B^2 -- underestimating B is catastrophic (par. 2.2 MATH_SPEC)
DEFAULT_BANKROLL = 50_000.0


@dataclass
class WhaleMetrics:
    """Calibrated whale metrics."""

    pnl: Decimal  # Realized PnL in USDC
    roi: Decimal  # ROI in percent (e.g. 25.5 = 25.5%)
    wins: int  # Number of winning positions
    losses: int  # Number of losing positions
    total_invested: Decimal  # Total invested capital
    total_positions: int  # Total positions (open + closed)
    timing_score_avg: float  # Average Timing Score across recent trades


class WhaleAnalyticsEngine:
    """
    Analytics engine for calculating whale metrics.

    Works on top of data from:
    - Polymarket Data API /positions (avgPrice, currentValue, redeemable, mergeable)
    - transactions table (amount, direction, price, block_timestamp)
    - whales table (expert_weight, wins, losses)

    Does NOT modify whale_watcher.py -- read-only.
    """

    # === PnL / ROI / Win-Loss (Sub-step 2.1) ===

    async def calculate_win_loss_from_db(self, address: str) -> tuple[int, int]:
        """
        W/L per-market из transactions + oracle_snapshots.

        Логика:
        1. Нормализация outcome через markets.token_outcomes маппинг
           (named outcomes типа "Michigan Wolverines" → yes/no)
        2. Нетто-позиция по yes/no для каждого resolved рынка
        3. net_yes > net_no → кит ставит на YES, иначе NO
        4. Сравниваем с oracle_snapshots.outcome
        5. |net_yes - net_no| ≤ 10 → SKIP (dust/арбитраж)
        """
        rows = await fetch(
            """
            WITH whale_positions AS (
                SELECT
                    t.market_id,
                    SUM(CASE WHEN t.direction = 'buy' AND (
                                LOWER(t.outcome) = 'yes'
                                OR t.outcome = m.token_outcomes->>'Yes'
                             ) THEN t.amount
                             WHEN t.direction = 'sell' AND (
                                LOWER(t.outcome) = 'yes'
                                OR t.outcome = m.token_outcomes->>'Yes'
                             ) THEN -t.amount
                             ELSE 0 END) AS net_yes_usd,
                    SUM(CASE WHEN t.direction = 'buy' AND (
                                LOWER(t.outcome) = 'no'
                                OR t.outcome = m.token_outcomes->>'No'
                             ) THEN t.amount
                             WHEN t.direction = 'sell' AND (
                                LOWER(t.outcome) = 'no'
                                OR t.outcome = m.token_outcomes->>'No'
                             ) THEN -t.amount
                             ELSE 0 END) AS net_no_usd
                FROM transactions t
                LEFT JOIN markets m ON m.id = t.market_id
                WHERE t.whale_id = (SELECT id FROM whales WHERE LOWER(address) = LOWER($1))
                GROUP BY t.market_id
            ),
            resolved AS (
                SELECT market_id, outcome
                FROM mv_market_resolved_outcome
                WHERE outcome IN ('yes', 'no')
            )
            SELECT
                wp.market_id,
                CASE WHEN wp.net_yes_usd > wp.net_no_usd THEN 'yes' ELSE 'no' END AS whale_bet,
                r.outcome AS actual_outcome,
                wp.net_yes_usd,
                wp.net_no_usd
            FROM whale_positions wp
            JOIN resolved r ON r.market_id = wp.market_id
            WHERE ABS(wp.net_yes_usd - wp.net_no_usd) > 10
            """,
            address,
        )

        wins = 0
        losses = 0
        for row in rows:
            if row["whale_bet"] == row["actual_outcome"]:
                wins += 1
            else:
                losses += 1

        return wins, losses

    async def calculate_pnl_from_db(self, address: str) -> tuple[Decimal, Decimal]:
        """
        Realized P&L и total_invested из transactions + oracle_snapshots.

        P&L per market = sell_proceeds + payout_for_winning_shares - buy_cost.
        Шеры = amount / price.

        Нормализация outcome: named outcomes (team names, Over/Under)
        маппятся в yes/no через markets.token_outcomes JSONB.

        Returns:
            (realized_pnl, total_invested) — оба в USDC.
        """
        rows = await fetch(
            """
            WITH whale_trades AS (
                SELECT
                    t.market_id,
                    t.direction,
                    CASE
                        WHEN LOWER(t.outcome) IN ('yes', 'no') THEN LOWER(t.outcome)
                        WHEN t.outcome = m.token_outcomes->>'Yes' THEN 'yes'
                        WHEN t.outcome = m.token_outcomes->>'No' THEN 'no'
                        ELSE LOWER(t.outcome)
                    END AS outcome,
                    SUM(t.amount) AS total_usd,
                    SUM(CASE WHEN t.price > 0 THEN t.amount / t.price ELSE 0 END) AS total_shares
                FROM transactions t
                LEFT JOIN markets m ON m.id = t.market_id
                WHERE t.whale_id = (SELECT id FROM whales WHERE LOWER(address) = LOWER($1))
                GROUP BY t.market_id, t.direction,
                    CASE
                        WHEN LOWER(t.outcome) IN ('yes', 'no') THEN LOWER(t.outcome)
                        WHEN t.outcome = m.token_outcomes->>'Yes' THEN 'yes'
                        WHEN t.outcome = m.token_outcomes->>'No' THEN 'no'
                        ELSE LOWER(t.outcome)
                    END
            ),
            resolved AS (
                SELECT market_id, outcome
                FROM mv_market_resolved_outcome
                WHERE outcome IN ('yes', 'no')
            ),
            market_pnl AS (
                SELECT
                    r.market_id,
                    COALESCE(SUM(wt.total_usd) FILTER (WHERE wt.direction = 'buy'), 0) AS total_buy_cost,
                    COALESCE(SUM(wt.total_usd) FILTER (WHERE wt.direction = 'sell'), 0) AS total_sell_proceeds,
                    COALESCE(SUM(wt.total_shares) FILTER (
                        WHERE wt.direction = 'buy' AND wt.outcome = r.outcome
                    ), 0) - COALESCE(SUM(wt.total_shares) FILTER (
                        WHERE wt.direction = 'sell' AND wt.outcome = r.outcome
                    ), 0) AS net_winning_shares
                FROM resolved r
                JOIN whale_trades wt ON wt.market_id = r.market_id
                GROUP BY r.market_id
            )
            SELECT
                SUM(total_sell_proceeds + GREATEST(net_winning_shares, 0) * 1.0 - total_buy_cost) AS realized_pnl,
                SUM(total_buy_cost) AS total_invested
            FROM market_pnl
            """,
            address,
        )

        if rows and rows[0].get("realized_pnl") is not None:
            return (
                Decimal(str(rows[0]["realized_pnl"])),
                Decimal(str(rows[0]["total_invested"] or 0)),
            )
        return Decimal("0"), Decimal("0")

    # === Timing Score (Sub-step 2.2) ===

    def calculate_timing_score(
        self,
        entry_price: float,
        current_price: float,
        hours_since_entry: float,
        direction: str = "buy",
        lambda_decay: float = 0.05,
    ) -> float:
        """
        Timing Score = (P_current - P_entry) * e^(-lambda * dt)

        For BUY: (current - entry) * decay
        For SELL: (entry - current) * decay (seller profits on decline)

        Returns:
            Timing Score in range [-1, 1]
        """
        decay = math.exp(-lambda_decay * hours_since_entry)

        if direction == "sell":
            raw = (entry_price - current_price) * decay
        else:
            raw = (current_price - entry_price) * decay

        # Clamp to [-1, 1]
        return max(-1.0, min(1.0, raw))

    def calculate_timing_scores_batch(
        self,
        transactions: list[dict],
        current_prices: dict[str, float],
    ) -> list[dict]:
        """
        Batch Timing Score calculation for a list of transactions.

        For each transaction:
        1. entry_price = transaction["price"]
        2. current_price = current_prices[transaction["market_id"]]
        3. dt = (now - transaction["block_timestamp"]) in hours
        4. direction = transaction["direction"]
        """
        now = datetime.now(UTC)
        results = []

        for tx in transactions:
            market_id = tx.get("market_id")
            current_price = current_prices.get(market_id)
            if current_price is None:
                continue

            entry_price = float(tx.get("price", 0))
            direction = tx.get("direction", "buy")

            ts = parse_timestamp(tx.get("block_timestamp") or tx.get("created_at"))
            if ts is None:
                continue

            elapsed = hours_between(ts, now)
            score = self.calculate_timing_score(entry_price, current_price, elapsed, direction)

            results.append(
                {
                    "tx_hash": tx.get("tx_hash"),
                    "timing_score": score,
                    "entry_price": entry_price,
                    "current_price": current_price,
                    "hours_elapsed": elapsed,
                }
            )

        return results

    # === Arbitrage Filtering (Sub-step 2.3) ===

    def detect_arbitrage(
        self,
        transactions: list[dict],
        time_window_minutes: int = 5,
        price_sum_tolerance: float = 0.02,
    ) -> list[str]:
        """
        Detect arbitrage trades.

        Logic:
        1. Group transactions by (from_address, market_id)
        2. In each group look for pairs BUY YES + BUY NO
        3. |timestamps| < time_window_minutes
        4. Sum of prices in [1.0 - tolerance, 1.0 + tolerance]
        """
        arb_hashes: set[str] = set()

        # Group by (from_address, market_id)
        groups: dict[tuple[str, str], list[dict]] = {}
        for tx in transactions:
            if tx.get("direction", "").lower() != "buy":
                continue  # SELL cannot be part of an arbitrage pair
            key = (tx.get("from_address", "").lower(), tx.get("market_id", ""))
            groups.setdefault(key, []).append(tx)

        for _key, group_txs in groups.items():
            # Split into YES and NO
            yes_txs = [t for t in group_txs if t.get("outcome", "").upper() == "YES"]
            no_txs = [t for t in group_txs if t.get("outcome", "").upper() == "NO"]

            for yes_tx in yes_txs:
                yes_ts = parse_timestamp(yes_tx.get("block_timestamp") or yes_tx.get("created_at"))
                yes_price = float(yes_tx.get("price", 0))

                for no_tx in no_txs:
                    no_ts = parse_timestamp(no_tx.get("block_timestamp") or no_tx.get("created_at"))
                    no_price = float(no_tx.get("price", 0))

                    # Time window check
                    if yes_ts is not None and no_ts is not None:
                        delta_seconds = abs((yes_ts - no_ts).total_seconds())
                        if delta_seconds > time_window_minutes * 60:
                            continue

                    # Price sum ~ 1.0 check
                    price_sum = yes_price + no_price
                    if abs(price_sum - 1.0) > price_sum_tolerance:
                        continue

                    # Size proportionality check (ratio in [0.5, 2.0])
                    yes_amount = float(yes_tx.get("amount", 0))
                    no_amount = float(no_tx.get("amount", 0))
                    if yes_amount > 0 and no_amount > 0:
                        ratio = min(yes_amount, no_amount) / max(yes_amount, no_amount)
                        if ratio < 0.5:
                            continue

                    # Arbitrage pair found
                    arb_hashes.add(yes_tx.get("tx_hash", ""))
                    arb_hashes.add(no_tx.get("tx_hash", ""))

        return list(arb_hashes)

    def filter_conviction_trades(self, transactions: list[dict]) -> list[dict]:
        """Remove arbitrage trades from list."""
        arb_hashes = set(self.detect_arbitrage(transactions))
        return [tx for tx in transactions if tx.get("tx_hash") not in arb_hashes]

    def is_arbitrageur(self, transactions: list[dict], threshold: float = 0.3) -> bool:
        """
        Determine if a wallet is an arbitrageur.
        If > threshold (30%) of all trades are arbitrage -> True.
        """
        if not transactions:
            return False
        arb_hashes = set(self.detect_arbitrage(transactions))
        arb_ratio = len(arb_hashes) / len(transactions)
        return arb_ratio > threshold

    # === Bayesian Input Preparation (Sub-step 2.5) ===

    def prepare_bayesian_input(
        self,
        whale_address: str,
        stake: Decimal,
        market_price: float,
        direction: str,
        outcome: str,
        bankroll_usd: float | None,
        wins: int,
        losses: int,
        entry_price: float,
        hours_since_entry: float,
        expert_weight: float = 0.5,
        roi_percent: float = 0.0,
        bet_count: int = 0,
        token_outcomes: dict[str, str] | None = None,
    ) -> dict:
        """
        Prepare input data for BayesianScoreCalculator (Step 3).

        Нормализует direction + outcome → effective_bet (YES/NO):
        - buy YES / sell NO → BET_YES (direction="buy")
        - buy NO / sell YES → BET_NO (direction="sell")
        - Named outcome (e.g. "Lakers") → резолвится через token_outcomes
        """
        b_est = bankroll_usd if bankroll_usd and bankroll_usd > 0 else DEFAULT_BANKROLL

        # Нормализация: определить token_side (YES/NO)
        outcome_lower = (outcome or "").lower()
        if outcome_lower in ("yes", "no"):
            token_side = outcome_lower
        elif token_outcomes:
            if outcome == token_outcomes.get("Yes"):
                token_side = "yes"
            elif outcome == token_outcomes.get("No"):
                token_side = "no"
            else:
                token_side = None
        else:
            token_side = None

        # effective_bet: что кит реально ставит
        if token_side == "yes":
            is_betting_yes = direction == "buy"
        elif token_side == "no":
            is_betting_yes = direction == "sell"
        else:
            # Fallback: старое поведение для unknown outcomes
            is_betting_yes = direction == "buy"

        normalized_direction = "buy" if is_betting_yes else "sell"

        # b = decimal odds - 1 = (1/price) - 1
        if is_betting_yes:
            safe_price = max(market_price, 0.01)
        else:
            safe_price = max(1.0 - market_price, 0.01)
        b = (1.0 / safe_price) - 1.0

        timing_score = self.calculate_timing_score(entry_price, market_price, hours_since_entry, normalized_direction)

        return {
            "whale_address": whale_address,
            "S": float(stake),
            "B_est": b_est,
            "b": b,
            "W": wins,
            "L": losses,
            "direction": normalized_direction,
            "outcome": outcome,
            "entry_price": entry_price,
            "current_price": market_price,
            "hours_since_entry": hours_since_entry,
            "timing_score": timing_score,
            "expert_weight": expert_weight,
            "roi_percent": roi_percent,
            "bet_count": bet_count,
        }

    def estimate_bankroll_from_history(
        self,
        stake: float,
        avg_bet_size: float | None = None,
        max_bet_size: float | None = None,
        total_volume: float | None = None,
    ) -> float:
        """
        Оценка bankroll из истории ставок (синхронная, без API).

        Эвристики:
        - avg_bet_size / 0.10 — средний трейдер ставит ~10% от банкролла
        - max_bet_size / 0.25 — макс. ставка ≈ 25% от банкролла (Kelly cap)
        - total_volume / 5.0 — за историю прокручивает ~5x банкролла

        Возвращает медиану оценок для устойчивости к выбросам.
        """
        estimates = [DEFAULT_BANKROLL]

        if avg_bet_size and avg_bet_size > 0:
            estimates.append(avg_bet_size / 0.10)
        if max_bet_size and max_bet_size > 0:
            estimates.append(max_bet_size / 0.25)
        if total_volume and total_volume > 0:
            estimates.append(total_volume / 5.0)

        valid = sorted(e for e in estimates if e > 0)
        return valid[len(valid) // 2]

    async def estimate_bankroll(self, address: str) -> float:
        """
        Оценка bankroll кита.

        Приоритет:
        1. fetch_portfolio_value — portfolio value напрямую
        2. DB history (avg_bet, max_bet, total_volume) — медиана эвристик
        3. DEFAULT_BANKROLL
        """
        from app.services.polymarket_poller_service import get_poller_service

        try:
            poller = get_poller_service()

            # Приоритет 1: portfolio value из DB
            portfolio_value = await poller.fetch_portfolio_value(address)
            if portfolio_value and portfolio_value > 0:
                return max(portfolio_value, DEFAULT_BANKROLL)

            # Приоритет 2: эвристики из whales таблицы
            whale_data = await fetchval(
                """
                SELECT json_build_object(
                    'avg_bet_size', avg_bet_size,
                    'max_bet_size', max_bet_size,
                    'total_volume', total_volume
                )
                FROM whales WHERE LOWER(address) = LOWER($1)
                """,
                address,
            )
            if whale_data:
                import json as _json

                data = _json.loads(whale_data) if isinstance(whale_data, str) else whale_data
                return self.estimate_bankroll_from_history(
                    stake=0,
                    avg_bet_size=float(data.get("avg_bet_size") or 0) or None,
                    max_bet_size=float(data.get("max_bet_size") or 0) or None,
                    total_volume=float(data.get("total_volume") or 0) or None,
                )

            return DEFAULT_BANKROLL
        except Exception as e:
            logger.warning("bankroll_estimate_failed", address=address, error=str(e))
            return DEFAULT_BANKROLL

    # === Full metrics computation for a whale (aggregates everything) ===

    async def compute_full_metrics(self, whale_address: str) -> dict:
        """
        Full metrics computation for a whale.

        Pipeline:
        1. calculate_win_loss_from_db() → W/L per-market
        2. calculate_pnl_from_db() → realized P&L + total_invested
        3. ROI = PnL / total_invested × 100
        4. Timing scores из DB
        5. Arbitrage detection из DB
        6. Update whale record
        """
        address = whale_address.lower()

        # 1-2. W/L и P&L из resolved рынков
        wins, losses = await self.calculate_win_loss_from_db(address)
        pnl, total_invested = await self.calculate_pnl_from_db(address)

        # 3. ROI
        roi = (pnl / total_invested * 100) if total_invested > 0 else Decimal("0")

        metrics = WhaleMetrics(
            pnl=pnl,
            roi=roi,
            wins=wins,
            losses=losses,
            total_invested=total_invested,
            total_positions=wins + losses,
            timing_score_avg=0.0,
        )

        # 4. Timing scores — 1 запрос с JOIN вместо 2
        tx_rows = await fetch(
            """
            SELECT t.market_id, t.price, t.direction, t.outcome, t.created_at,
                   t.tx_hash, t.from_address, m.current_price
            FROM transactions t
            LEFT JOIN markets m ON m.id = t.market_id
            WHERE t.from_address = $1
            ORDER BY t.created_at DESC
            LIMIT 200
            """,
            address,
        )
        transactions = records_to_list(tx_rows)

        current_prices: dict[str, float] = {
            str(tx["market_id"]): float(tx["current_price"])
            for tx in transactions
            if tx.get("current_price") is not None and tx.get("market_id")
        }

        timing_scores = self.calculate_timing_scores_batch(transactions, current_prices)
        timing_avg = sum(ts["timing_score"] for ts in timing_scores) / len(timing_scores) if timing_scores else 0.0
        metrics.timing_score_avg = timing_avg

        # 5. Arbitrage detection
        arb_hashes = set(self.detect_arbitrage(transactions))
        arb_ratio = len(arb_hashes) / len(transactions) if transactions else 0.0
        is_arb = arb_ratio > 0.3

        # 6. Bet size stats
        amounts = [float(tx.get("amount", 0)) for tx in transactions if float(tx.get("amount", 0)) > 0]
        avg_bet = sum(amounts) / len(amounts) if amounts else 0.0
        max_bet = max(amounts) if amounts else 0.0
        bet_count = len(amounts)
        whale_tier = get_whale_tier(avg_bet, bet_count)

        # 7. Fresh wallet risk (Phoenix V2: упрощённая логика без smart_money)
        # Раньше: FreshWalletDetector с ML-калибровкой. Сейчас — простое правило:
        # fresh = wallet age < 7 дней + bet_count < 10 + max_bet > $5K (suspicious).
        whale_row = await fetchrow(
            "SELECT created_at FROM whales WHERE LOWER(address) = $1",
            address,
        )
        first_seen = whale_row["created_at"] if whale_row else datetime.now(UTC)
        wallet_age_days = (datetime.now(UTC) - first_seen).days
        fresh_risk = 0.8 if (wallet_age_days < 7 and bet_count < 10 and max_bet > 5000) else 0.0

        # 8. Update whale record in DB
        now_iso = datetime.now(UTC)
        try:
            await execute(
                """
                UPDATE whales
                SET pnl_usd = $1,
                    roi_percent = $2,
                    wins = $3,
                    losses = $4,
                    total_invested = $5,
                    timing_score_avg = $6,
                    is_arbitrageur = $7,
                    arb_ratio = $8,
                    metrics_updated_at = $9,
                    avg_bet_size = $10,
                    max_bet_size = $11,
                    bet_count = $12,
                    whale_tier = $13,
                    fresh_wallet_risk = $14,
                    win_rate = $16
                WHERE address = $15
                """,
                str(pnl),
                str(roi),
                wins,
                losses,
                str(total_invested),
                round(timing_avg, 4),
                is_arb,
                round(arb_ratio, 4),
                now_iso,
                round(avg_bet, 2),
                round(max_bet, 2),
                bet_count,
                whale_tier,
                round(fresh_risk, 4),
                address,
                wins / max(wins + losses, 1),
            )
        except Exception as e:
            logger.error("whale_metrics_update_failed", address=address, error=str(e))

        logger.info(
            "whale_full_metrics_computed",
            address=address,
            pnl=str(pnl),
            roi=str(roi),
            wins=wins,
            losses=losses,
            timing_avg=timing_avg,
            arb_ratio=arb_ratio,
            avg_bet_size=round(avg_bet, 2),
            max_bet_size=round(max_bet, 2),
            bet_count=bet_count,
            whale_tier=whale_tier,
        )

        return {
            "metrics": metrics,
            "is_arbitrageur": is_arb,
            "arb_ratio": arb_ratio,
            "timing_scores": timing_scores,
            "avg_bet_size": round(avg_bet, 2),
            "max_bet_size": round(max_bet, 2),
            "bet_count": bet_count,
            "whale_tier": whale_tier,
        }


# === Helper functions ===


def parse_timestamp(ts: str | int | datetime | None) -> datetime | None:
    """
    Parse timestamp from various formats.

    Polymarket API may return:
    - Unix timestamp (int): 1704067200
    - ISO 8601 string: "2025-01-01T00:00:00Z"
    - datetime object (from asyncpg)
    - None
    """
    if ts is None:
        return None

    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=UTC)

    if isinstance(ts, int):
        return datetime.fromtimestamp(ts, tz=UTC)

    if isinstance(ts, str):
        # ISO 8601
        ts_clean = ts.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(ts_clean)
        except ValueError:
            logger.warning("timestamp_parse_failed", value=ts)
            return None

    return None


def hours_between(start: datetime, end: datetime | None = None) -> float:
    """Difference in hours between two datetimes."""
    if end is None:
        end = datetime.now(UTC)
    delta = end - start
    return delta.total_seconds() / 3600.0


# Singleton
_analytics_engine: WhaleAnalyticsEngine | None = None


def get_whale_analytics_engine() -> WhaleAnalyticsEngine:
    global _analytics_engine
    if _analytics_engine is None:
        _analytics_engine = WhaleAnalyticsEngine()
    return _analytics_engine
