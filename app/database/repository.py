"""
Database repository — all database read/write operations.

Uses SQLAlchemy 2.x async sessions.

Duplicate protection is implemented via:
  1. Database UNIQUE constraint on (provider, chain, contract_address, external_id)
  2. IntegrityError catch in save_mint() → returns existing record instead of duplicate
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    BroadcastTargetDB,
    MintOpportunityDB,
    NotificationDB,
    SystemStateDB,
)
from app.models.mint import MintOpportunity, MintStatus

log = logging.getLogger(__name__)


class MintRepository:
    """All database operations for mint opportunities."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_mint(self, mint: MintOpportunity) -> tuple[MintOpportunityDB, bool]:
        """
        Insert a mint record. Returns (record, is_new).

        If the unique constraint fires (duplicate), returns the existing
        record with is_new=False without raising an exception.
        """
        db_record = _mint_to_db(mint)
        self._session.add(db_record)
        try:
            await self._session.flush()
            log.info(
                "[MINT_DISCOVERED] id=%s project=%r chain=%s external_id=%s",
                db_record.id,
                db_record.project_name,
                db_record.chain,
                db_record.external_id,
            )
            return db_record, True
        except IntegrityError:
            await self._session.rollback()
            existing = await self.get_by_external_id(
                mint.provider, mint.chain, mint.contract_address, mint.external_id
            )
            log.debug(
                "[MINT_DUPLICATE] external_id=%s chain=%s",
                mint.external_id,
                mint.chain,
            )
            return existing, False  # type: ignore[return-value]

    async def get_by_external_id(
        self,
        provider: str,
        chain: str,
        contract_address: str,
        external_id: str,
    ) -> Optional[MintOpportunityDB]:
        stmt = select(MintOpportunityDB).where(
            MintOpportunityDB.provider == provider,
            MintOpportunityDB.chain == chain,
            MintOpportunityDB.contract_address == contract_address,
            MintOpportunityDB.external_id == external_id,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def update_status(
        self, mint_id: str, status: MintStatus, extra: Optional[dict] = None
    ) -> None:
        values = {
            "status": status.value,
            "updated_at": datetime.now(timezone.utc),
        }
        if extra:
            values.update(extra)
        stmt = (
            update(MintOpportunityDB)
            .where(MintOpportunityDB.id == mint_id)
            .values(**values)
        )
        await self._session.execute(stmt)

    async def get_mints_by_status(self, status: MintStatus) -> list[MintOpportunityDB]:
        stmt = select(MintOpportunityDB).where(
            MintOpportunityDB.status == status.value
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_pending_for_notification(self) -> list[MintOpportunityDB]:
        """Return SCHEDULED mints whose notification_scheduled_at has passed."""
        now = datetime.now(timezone.utc)
        stmt = select(MintOpportunityDB).where(
            MintOpportunityDB.status == MintStatus.SCHEDULED.value,
            MintOpportunityDB.notification_scheduled_at <= now,
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_pending_for_deletion(self) -> list[MintOpportunityDB]:
        """Return NOTIFIED mints whose delete_scheduled_at has passed."""
        now = datetime.now(timezone.utc)
        stmt = select(MintOpportunityDB).where(
            MintOpportunityDB.status.in_(
                [MintStatus.NOTIFIED.value, MintStatus.DELETE_PENDING.value]
            ),
            MintOpportunityDB.delete_scheduled_at <= now,
            MintOpportunityDB.notification_message_id.is_not(None),
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())


class NotificationRepository:
    """All database operations for notification records."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_notification(self, notif: NotificationDB) -> None:
        self._session.add(notif)
        await self._session.flush()

    async def get_by_mint_id(self, mint_id: str) -> Optional[NotificationDB]:
        stmt = select(NotificationDB).where(NotificationDB.mint_id == mint_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_all_by_mint_id(self, mint_id: str) -> list[NotificationDB]:
        stmt = select(NotificationDB).where(NotificationDB.mint_id == mint_id)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())


class TargetRepository:
    """Operations for broadcast targets (groups/channels/topics)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_all_active(self) -> list[BroadcastTargetDB]:
        stmt = select(BroadcastTargetDB).where(BroadcastTargetDB.is_active == True)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def add_target(
        self, chat_id: int, topic_id: Optional[int] = None, label: Optional[str] = None
    ) -> BroadcastTargetDB:
        stmt = select(BroadcastTargetDB).where(
            BroadcastTargetDB.chat_id == chat_id,
            BroadcastTargetDB.topic_id == topic_id,
        )
        result = await self._session.execute(stmt)
        target = result.scalar_one_or_none()
        if target:
            target.is_active = True
            if label:
                target.label = label
            return target

        target = BroadcastTargetDB(
            chat_id=chat_id,
            topic_id=topic_id,
            label=label,
            is_active=True,
        )
        self._session.add(target)
        await self._session.flush()
        return target

    async def remove_target(
        self, chat_id: int, topic_id: Optional[int] = None
    ) -> bool:
        stmt = select(BroadcastTargetDB).where(
            BroadcastTargetDB.chat_id == chat_id,
            BroadcastTargetDB.topic_id == topic_id,
        )
        result = await self._session.execute(stmt)
        target = result.scalar_one_or_none()
        if target:
            await self._session.delete(target)
            await self._session.flush()
            return True
        return False


class SystemStateRepository:
    """Key-value persistent state store."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, key: str) -> Optional[str]:
        stmt = select(SystemStateDB).where(SystemStateDB.key == key)
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        return row.value if row else None

    async def set(self, key: str, value: str) -> None:
        stmt = select(SystemStateDB).where(SystemStateDB.key == key)
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row:
            row.value = value
            row.updated_at = datetime.now(timezone.utc)
        else:
            self._session.add(
                SystemStateDB(
                    key=key,
                    value=value,
                    updated_at=datetime.now(timezone.utc),
                )
            )
        await self._session.flush()


# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------

def _mint_to_db(mint: MintOpportunity) -> MintOpportunityDB:
    return MintOpportunityDB(
        id=mint.id,
        provider=mint.provider,
        external_id=mint.external_id,
        collection_slug=mint.collection_slug,
        project_name=mint.project_name,
        chain=mint.chain,
        contract_address=mint.contract_address,
        mint_type=mint.mint_type,
        mint_price_eth=str(mint.mint_price_eth) if mint.mint_price_eth is not None else None,
        mint_price_usd=str(mint.mint_price_usd),
        offer_price_usd=str(mint.offer_price_usd) if mint.offer_price_usd is not None else None,
        total_supply=mint.total_supply,
        minted_quantity=mint.minted_quantity,
        minted_percentage=str(mint.minted_percentage) if mint.minted_percentage is not None else None,
        mint_start_time=mint.mint_start_time,
        mint_end_time=mint.mint_end_time,
        mint_url=mint.mint_url,
        notification_scheduled_at=mint.notification_scheduled_at,
        notification_sent_at=mint.notification_sent_at,
        notification_message_id=mint.notification_message_id,
        delete_scheduled_at=mint.delete_scheduled_at,
        deleted_at=mint.deleted_at,
        status=str(mint.status),
        detected_at=mint.detected_at,
        created_at=mint.created_at,
        updated_at=mint.updated_at,
    )
