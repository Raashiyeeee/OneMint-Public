"""
Mint service — high-level business operations.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import MintOpportunityDB
from app.database.repository import MintRepository
from app.models.mint import MintStatus

log = logging.getLogger(__name__)


class MintService:
    """Business logic operations on mint records."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = MintRepository(session)

    async def get_pending_notifications(self) -> list[MintOpportunityDB]:
        """Return mints scheduled for notification whose time has arrived."""
        return await self._repo.get_pending_for_notification()

    async def get_pending_deletions(self) -> list[MintOpportunityDB]:
        """Return notified mints whose deletion time has arrived."""
        return await self._repo.get_pending_for_deletion()

    async def mark_notified(
        self, mint_id: str, message_id: int, delete_at: datetime
    ) -> None:
        await self._repo.update_status(
            mint_id,
            MintStatus.NOTIFIED,
            {
                "notification_sent_at": datetime.now(timezone.utc),
                "notification_message_id": message_id,
                "delete_scheduled_at": delete_at,
            },
        )

    async def mark_deleted(self, mint_id: str) -> None:
        await self._repo.update_status(
            mint_id,
            MintStatus.DELETED,
            {"deleted_at": datetime.now(timezone.utc)},
        )
