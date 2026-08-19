#!/usr/bin/env python3
"""Build external_odds.parquet — cross-source consensus labels для Sports + Crypto markets.

Sources:
- The Odds API (free tier 500 req/month) — Pinnacle closing lines для Sports
- CoinGecko (free) — BTC/ETH spot price для Crypto markets

Output (/mnt/data/canonical/external_odds.parquet):
  condition_id     VARCHAR
  source           VARCHAR  ('pinnacle' | 'coingecko')
  external_price   DOUBLE   (implied probability)
  fetched_at       BIGINT
  divergence_vs_polymarket DOUBLE  (external - polymarket_yes_price)

Backfill: для resolved markets (matched через question text + end_date).
Online: live capture для open markets через cron.

Note: для MVP используем The Odds API только для US sports (NBA, NFL, MLB, NHL).
Polymarket Sports — 14K markets, фильтруем по category='Sports'.

Запуск (one-shot backfill):
    cd ~/oracle && /root/oracle/.venv-research/bin/python scripts/research/build_external_odds.py --backfill

Запуск (live update):
    cd ~/oracle && /root/oracle/.venv-research/bin/python scripts/research/build_external_odds.py --live
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

logger = logging.getLogger(__name__)

# Placeholder — full implementation требует API keys
# The Odds API: https://the-odds-api.com — free tier ключ через signup
# CoinGecko: https://www.coingecko.com/api — free tier no auth для базовых endpoints

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
ODDS_API_BASE = "https://api.the-odds-api.com/v4"
COINGECKO_BASE = "https://api.coingecko.com/api/v3"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", action="store_true", help="One-shot historical backfill")
    parser.add_argument("--live", action="store_true", help="Live update for open markets")
    args = parser.parse_args()

    if not args.backfill and not args.live:
        parser.print_help()
        return 1

    if not ODDS_API_KEY:
        print("ERROR: ODDS_API_KEY env var not set")
        print("Sign up: https://the-odds-api.com/")
        return 1

    print("STUB — full implementation pending after wallet/microstructure phases land")
    print("See plan file: <local-plans>/ml-async-flask.md Phase E")
    return 0


if __name__ == "__main__":
    sys.exit(main())
