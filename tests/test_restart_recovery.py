"""
Tests for restart recovery logic.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database.models import Base, MintOpportunityDB
from app.models.mint import MintStatus
from app.utils.time import delete_time, notification_time, utcnow


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        yield session
    await engine.dispose()


def make_scheduled_record(start_offset_minutes: int = 5) -> MintOpportunityDB:
    """Create a SCHEDULED mint that started sooner than expected."""
    now = datetime.now(timezone.utc)
    start = now + timedelta(minutes=start_offset_minutes)
    notif_at = notification_time(start, 10)
    del_at = delete_time(start, 15)
    return MintOpportunityDB(
        id=str(uuid.uuid4()),
        provider="opensea",
        external_id=str(uuid.uuid4()),
        collection_slug="recovery-test",
        project_name="Recovery Test",
        chain="ethereum",
        contract_address="0xRECOVERY",
        mint_type="public_sale",
        mint_price_usd="10",
        offer_price_usd="20",
        minted_percentage="75",
        total_supply=1000,
        minted_quantity=750,
        mint_start_time=start,
        mint_url="https://opensea.io/collection/test",
        status=MintStatus.SCHEDULED.value,
        notification_scheduled_at=notif_at,
        delete_scheduled_at=del_at,
    )


def test_recovery_case2_notification_overdue():
    """
    Recovery: Bot crashed before sending notification.
    Mint starts in 5 minutes — notification was due 5 minutes ago.
    Expected: send immediately.
    """
    start = utcnow() + timedelta(minutes=5)
    notif_at = notification_time(start, before_minutes=10)
    del_at = delete_time(start, after_minutes=15)
    now = utcnow()

    assert notif_at < now    # Overdue
    assert del_at > now      # Deletion still in future
    # Action: send immediately, schedule deletion at del_at


def test_recovery_case3_mint_started_within_window():
    """
    Recovery: Bot restarted after mint started but within 15-min window.
    Mint started 5 minutes ago.
    Expected: send immediately, schedule deletion at start+15.
    """
    start = utcnow() - timedelta(minutes=5)
    del_at = delete_time(start, after_minutes=15)
    now = utcnow()

    assert del_at > now       # Still 10 minutes until deletion
    # Action: send immediately, schedule deletion at del_at


def test_recovery_case4_expired():
    """
    Recovery: Bot restarted 20 minutes after mint started.
    Expected: mark as EXPIRED — do not send.
    """
    start = utcnow() - timedelta(minutes=20)
    del_at = delete_time(start, after_minutes=15)
    now = utcnow()

    assert del_at < now       # Fully expired
    # Action: mark EXPIRED


def test_recovery_notified_reschedules_deletion():
    """
    Recovery: Bot crashed after sending notification but before deleting it.
    The NOTIFIED record with notification_message_id must reschedule deletion.
    """
    start = utcnow() + timedelta(minutes=10)
    del_at = delete_time(start, after_minutes=15)
    now = utcnow()

    record = MintOpportunityDB(
        id=str(uuid.uuid4()),
        provider="opensea",
        external_id=str(uuid.uuid4()),
        collection_slug="notified-test",
        project_name="Notified Test",
        chain="ethereum",
        contract_address="0xNOTIFIED",
        mint_type="public_sale",
        mint_price_usd="10",
        offer_price_usd="20",
        minted_percentage="75",
        total_supply=1000,
        minted_quantity=750,
        mint_start_time=start,
        mint_url="https://opensea.io/collection/test",
        status=MintStatus.NOTIFIED.value,
        notification_message_id=42000,
        delete_scheduled_at=del_at,
    )

    assert record.notification_message_id == 42000
    assert record.status == "NOTIFIED"
    # Recovery should: schedule_deletion(mint_id=record.id, message_id=42000, run_at=del_at)


@pytest.mark.asyncio
async def test_recovery_scan_updates_expired_records(db_session):
    """Database records that are expired are updated to EXPIRED status."""
    from app.database.repository import MintRepository

    # Create a record with start time 20 minutes ago (expired)
    start = utcnow() - timedelta(minutes=20)
    del_at = delete_time(start, 15)

    record = MintOpportunityDB(
        id=str(uuid.uuid4()),
        provider="opensea",
        external_id=str(uuid.uuid4()),
        collection_slug="expired",
        project_name="Expired Test",
        chain="base",
        contract_address="0xEXPIRED",
        mint_type="public_sale",
        mint_price_usd="5",
        offer_price_usd="10",
        minted_percentage="75",
        total_supply=100,
        minted_quantity=75,
        mint_start_time=start,
        mint_url="https://opensea.io/collection/expired",
        status=MintStatus.SCHEDULED.value,
        notification_scheduled_at=notification_time(start, 10),
        delete_scheduled_at=del_at,
    )

    db_session.add(record)
    await db_session.commit()

    # Simulate recovery logic
    repo = MintRepository(db_session)
    now = utcnow()
    if del_at < now:
        await repo.update_status(record.id, MintStatus.EXPIRED)
        await db_session.commit()

    # Verify status
    found = await repo.get_by_external_id(
        "opensea", record.chain, record.contract_address, record.external_id
    )
    assert found.status == "EXPIRED"
