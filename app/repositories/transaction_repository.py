"""
Transaction Repository — data access layer for transactions and whales tables.

asyncpg-based, uses global connection pool from app.services.database.
"""

from decimal import Decimal
from typing import Any

from app.repositories.base import BaseRepository
from app.services.database import execute, fetchrow, fetchval, record_to_dict


class TransactionRepository(BaseRepository):
    def __init__(self) -> None:
        pass

    async def create(self, transaction_data: dict[str, Any]) -> dict[str, Any]:
        """Create a new transaction record."""
        # Ensure amount is string for precision
        if "amount" in transaction_data and isinstance(transaction_data["amount"], Decimal):
            transaction_data["amount"] = str(transaction_data["amount"])

        return await self._create("transactions", transaction_data)

    async def get_whale_by_address(self, address: str) -> dict[str, Any] | None:
        """Get whale profile by address."""
        row = await fetchrow(
            "SELECT * FROM whales WHERE address = $1",
            address,
        )
        return record_to_dict(row)

    async def create_whale(self, whale_data: dict[str, Any]) -> dict[str, Any]:
        """Create a new whale profile."""
        return await self._create("whales", whale_data)

    async def update_whale_volume(self, whale_id: str, new_volume: Decimal) -> None:
        """Update whale total volume."""
        await execute(
            """
            UPDATE whales
            SET total_volume = $1, last_seen_at = now()
            WHERE id = $2
            """,
            str(new_volume),
            whale_id,
        )

    async def get_whale_volume(self, whale_id: str) -> Decimal:
        """Get current volume for a whale."""
        val = await fetchval(
            "SELECT total_volume FROM whales WHERE id = $1",
            whale_id,
        )
        if val is None:
            return Decimal(0)
        return Decimal(val)
