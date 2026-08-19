"""The Graph Polymarket Subgraph — resolution detection через on-chain redemptions."""

import httpx
import structlog

from app.core.config import get_settings
from app.core.retry import http_retry

logger = structlog.get_logger()

SUBGRAPH_ID = "Bx1W4SvPYKMBVRLRGp3cUDmRSTd3AEQKRGiF2XQq3mPG"

_REDEMPTION_QUERY = """
query CheckRedemption($condition: String!) {
    redemptions(where: {condition: $condition}, first: 1) {
        payer
        collateralToken
        indexSets
    }
}
"""


class TheGraphService:
    """GraphQL клиент для The Graph — проверка on-chain resolution."""

    def __init__(self):
        settings = get_settings()
        self._api_key = settings.thegraph_api_key
        self._url = f"https://gateway.thegraph.com/api/{self._api_key}/subgraphs/id/{SUBGRAPH_ID}"

    @http_retry
    async def _fetch_redemptions(self, condition_id: str) -> dict:
        """Один GraphQL-запрос; transient ошибки (timeout/connect/5xx/429)
        ретраятся http_retry (3 попытки, backoff 2→4→8с), затем reraise."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                self._url,
                json={"query": _REDEMPTION_QUERY, "variables": {"condition": condition_id}},
            )
            response.raise_for_status()
            return response.json()

    async def check_market_resolved(self, condition_id: str) -> bool:
        """Проверить есть ли redemption events для condition_id. Returns True если resolved on-chain.

        return False после исчерпания ретраев — осознанный fallback: это уровень 3
        resolve-каскада (resolve_outcomes.py), недоступность The Graph не должна
        блокировать каскад, рынок доберёт уровень 4 (price proxy).
        """
        if not self._api_key:
            return False

        try:
            data = await self._fetch_redemptions(condition_id)

            redemptions = data.get("data", {}).get("redemptions", [])
            resolved = len(redemptions) > 0

            if resolved:
                logger.info("thegraph_market_resolved", condition_id=condition_id[:20])

            return resolved
        except Exception as e:
            logger.warning("thegraph_check_failed", condition_id=condition_id[:20], error=str(e))
            return False
