"""
Tests for discovery notifications and manual filtering mode.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings
from app.database.database import init_engine
from app.database.models import Base, MintOpportunityDB
from app.database.repository import MintRepository
from app.models.mint import MintOpportunity, MintStatus
from app.monitor.processor import MintProcessor
from app.providers.base import RawDrop, RawDropStage
from app.services.notification_service import TelegramNotificationService


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        yield session
    await engine.dispose()


def make_settings(notify_all_discovered: bool = True) -> Settings:
    return Settings(
        telegram_bot_token="fake:token",
        telegram_group_id=-100123456,
        public_mint_topic_id=156,
        opensea_api_key="fake-key",
        max_mint_price_usd=Decimal("20"),
        notify_all_discovered=notify_all_discovered,
    )


def make_raw_drop_and_stage(price_wei: str = "10000000000000000000"):  # 10 ETH (~$25,000) -> rejected by max_price
    now = datetime.now(timezone.utc)
    stage = RawDropStage(
        uuid=str(uuid.uuid4()),
        stage_type="public_sale",
        label="Public Mint",
        price=price_wei,
        price_currency_address="0x0000000000000000000000000000000000000000",
        start_time=(now + timedelta(hours=2)).isoformat(),
        end_time=(now + timedelta(hours=3)).isoformat(),
        max_per_wallet="5",
    )
    raw = RawDrop(
        chain="ethereum",
        contract_address="0x1234567890abcdef1234567890abcdef12345678",
        collection_slug="high-price-nft",
        collection_name="High Price NFT",
        drop_type="seadrop_v1_erc721",
        is_minting=False,
        opensea_url="https://opensea.io/collection/high-price-nft",
        total_supply="800",
        max_supply="1000",
        active_stage=stage,
    )
    return raw, stage


@pytest.mark.asyncio
async def test_notify_all_discovered_sends_notification_for_rejected_mint(db_session):
    """When notify_all_discovered=True, mints rejected by auto-filter are still alerted immediately."""
    settings = make_settings(notify_all_discovered=True)
    raw, stage = make_raw_drop_and_stage()

    mock_provider = AsyncMock()
    mock_provider.get_mint_details.return_value = None
    mock_provider.get_collection_floor_price.return_value = None
    # Normalize mint returns opportunity that fails max price rule
    mint_opp = MintOpportunity(
        external_id=stage.uuid,
        project_name=raw.collection_name,
        chain=raw.chain,
        contract_address=raw.contract_address,
        mint_type="public_sale",
        mint_price_usd=Decimal("50.00"),  # > max_mint_price_usd=20
        mint_price_eth=Decimal("0.02"),
        offer_price_usd=Decimal("100.00"),
        minted_percentage=Decimal("80.00"),
        total_supply=1000,
        minted_quantity=800,
        mint_start_time=datetime.now(timezone.utc) + timedelta(hours=2),
        mint_url="https://opensea.io/collection/high-price-nft",
        collection_slug=raw.collection_slug,
    )
    mock_provider.normalize_mint.return_value = mint_opp

    mock_scheduler = MagicMock()
    mock_notifier = AsyncMock()
    mock_notifier.send_to_target.return_value = 99999

    processor = MintProcessor(
        settings=settings,
        provider=mock_provider,
        session=db_session,
        scheduler=mock_scheduler,
        notifier=mock_notifier,
        eth_price_usd=Decimal("2500"),
    )

    await processor.process(raw, stage)

    # Verification: notifier sent message despite filter rejecting max price
    assert mock_notifier.send_to_target.called
    called_mint = mock_notifier.send_to_target.call_args[0][0]
    assert called_mint.project_name == "High Price NFT"
    assert "mint_price_usd" in called_mint.rejection_reason


@pytest.mark.asyncio
async def test_notify_all_discovered_false_does_not_send_rejected_mint(db_session):
    """When notify_all_discovered=False, mints failing filter are rejected and NOT alerted."""
    settings = make_settings(notify_all_discovered=False)
    raw, stage = make_raw_drop_and_stage()

    mock_provider = AsyncMock()
    mock_provider.get_mint_details.return_value = None
    mock_provider.get_collection_floor_price.return_value = None

    mint_opp = MintOpportunity(
        external_id=stage.uuid,
        project_name=raw.collection_name,
        chain=raw.chain,
        contract_address=raw.contract_address,
        mint_type="public_sale",
        mint_price_usd=Decimal("50.00"),  # > max_mint_price_usd=20
        mint_start_time=datetime.now(timezone.utc) + timedelta(hours=2),
        mint_url="https://opensea.io/collection/high-price-nft",
        collection_slug=raw.collection_slug,
    )
    mock_provider.normalize_mint.return_value = mint_opp

    mock_scheduler = MagicMock()
    mock_notifier = AsyncMock()

    processor = MintProcessor(
        settings=settings,
        provider=mock_provider,
        session=db_session,
        scheduler=mock_scheduler,
        notifier=mock_notifier,
        eth_price_usd=Decimal("2500"),
    )

    await processor.process(raw, stage)

    # Verification: notifier NOT called
    assert not mock_notifier.send_to_target.called


@pytest.mark.asyncio
async def test_notify_all_discovered_does_not_renotify_duplicate(db_session):
    """When a mint is already saved in the database, it is not alerted again on subsequent polls."""
    settings = make_settings(notify_all_discovered=True)
    raw, stage = make_raw_drop_and_stage()

    mock_provider = AsyncMock()
    mock_provider.get_mint_details.return_value = None
    mock_provider.get_collection_floor_price.return_value = None

    mint_opp = MintOpportunity(
        external_id=stage.uuid,
        project_name=raw.collection_name,
        chain=raw.chain,
        contract_address=raw.contract_address,
        mint_type="public_sale",
        mint_price_usd=Decimal("50.00"),
        mint_start_time=datetime.now(timezone.utc) + timedelta(hours=2),
        mint_url="https://opensea.io/collection/high-price-nft",
        collection_slug=raw.collection_slug,
    )
    mock_provider.normalize_mint.return_value = mint_opp

    mock_scheduler = MagicMock()
    mock_notifier = AsyncMock()
    mock_notifier.send_to_target.return_value = 11111

    processor = MintProcessor(
        settings=settings,
        provider=mock_provider,
        session=db_session,
        scheduler=mock_scheduler,
        notifier=mock_notifier,
        eth_price_usd=Decimal("2500"),
    )

    # First discovery
    await processor.process(raw, stage)
    assert mock_notifier.send_to_target.call_count == 1

    # Second poll (duplicate)
    mock_notifier.send_to_target.reset_mock()
    await processor.process(raw, stage)
    assert mock_notifier.send_to_target.call_count == 0
