"""
Tests for deduplication — verifying that duplicate mints are correctly
identified and not processed twice.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.database.database import create_tables, init_engine
from app.database.models import Base
from app.database.repository import MintRepository
from app.models.mint import MintOpportunity, MintStatus


@pytest_asyncio.fixture
async def db_session():
    """In-memory SQLite session for deduplication tests."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        yield session
    await engine.dispose()


def make_mint(
    external_id: str | None = None,
    chain: str = "ethereum",
    contract: str = "0xABC123",
) -> MintOpportunity:
    now = datetime.now(timezone.utc)
    return MintOpportunity(
        external_id=external_id or str(uuid.uuid4()),
        project_name="Dedup Test",
        chain=chain,
        contract_address=contract,
        mint_type="public_sale",
        mint_price_usd=Decimal("10"),
        offer_price_usd=Decimal("20"),
        minted_percentage=Decimal("75"),
        total_supply=1000,
        minted_quantity=750,
        mint_start_time=now + timedelta(minutes=15),
        mint_url="https://opensea.io/collection/dedup",
        collection_slug="dedup",
    )


@pytest.mark.asyncio
async def test_first_save_is_new(db_session):
    """First save of a mint returns is_new=True."""
    repo = MintRepository(db_session)
    mint = make_mint(external_id="stage-abc-123")

    record, is_new = await repo.save_mint(mint)
    await db_session.commit()

    assert is_new is True
    assert record.external_id == "stage-abc-123"


@pytest.mark.asyncio
async def test_duplicate_save_is_not_new(db_session):
    """Second save of identical (provider, chain, contract, external_id) returns is_new=False."""
    repo = MintRepository(db_session)
    ext_id = "duplicate-stage-uuid"

    mint1 = make_mint(external_id=ext_id)
    _, is_new1 = await repo.save_mint(mint1)
    await db_session.commit()

    mint2 = make_mint(external_id=ext_id)  # Same identity
    _, is_new2 = await repo.save_mint(mint2)

    assert is_new1 is True
    assert is_new2 is False


@pytest.mark.asyncio
async def test_different_external_id_is_new(db_session):
    """Different external_id for same contract is a new record."""
    repo = MintRepository(db_session)

    mint1 = make_mint(external_id="stage-001")
    mint2 = make_mint(external_id="stage-002")

    _, is_new1 = await repo.save_mint(mint1)
    await db_session.commit()

    _, is_new2 = await repo.save_mint(mint2)
    await db_session.commit()

    assert is_new1 is True
    assert is_new2 is True


@pytest.mark.asyncio
async def test_same_stage_different_chain_is_new(db_session):
    """Same external_id but different chain is treated as a new record."""
    repo = MintRepository(db_session)
    ext_id = "shared-stage-id"

    mint_eth = make_mint(external_id=ext_id, chain="ethereum", contract="0xETH")
    mint_base = make_mint(external_id=ext_id, chain="base", contract="0xBASE")

    _, is_new1 = await repo.save_mint(mint_eth)
    await db_session.commit()

    _, is_new2 = await repo.save_mint(mint_base)
    await db_session.commit()

    assert is_new1 is True
    assert is_new2 is True


@pytest.mark.asyncio
async def test_get_by_external_id(db_session):
    """get_by_external_id returns the correct record."""
    repo = MintRepository(db_session)
    mint = make_mint(external_id="lookup-test-id")

    await repo.save_mint(mint)
    await db_session.commit()

    found = await repo.get_by_external_id(
        provider="opensea",
        chain=mint.chain,
        contract_address=mint.contract_address,
        external_id="lookup-test-id",
    )

    assert found is not None
    assert found.external_id == "lookup-test-id"


@pytest.mark.asyncio
async def test_status_update(db_session):
    """Status transitions are persisted correctly."""
    repo = MintRepository(db_session)
    mint = make_mint()

    record, _ = await repo.save_mint(mint)
    await db_session.commit()

    await repo.update_status(record.id, MintStatus.SCHEDULED)
    await db_session.commit()

    found = await repo.get_by_external_id(
        "opensea", mint.chain, mint.contract_address, mint.external_id
    )
    assert found.status == "SCHEDULED"
