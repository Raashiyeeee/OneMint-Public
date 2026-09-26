"""
Tests for API parser / provider normalisation.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.providers.base import RawDrop, RawDropStage
from app.providers.opensea import OpenSeaProvider, _parse_dt


def make_stage(
    stage_type: str = "public_sale",
    price_wei: str = "10000000000000000",  # 0.01 ETH
    start_offset_minutes: int = 15,
) -> RawDropStage:
    now = datetime.now(timezone.utc)
    start = now + timedelta(minutes=start_offset_minutes)
    end = start + timedelta(hours=1)
    return RawDropStage(
        uuid=str(uuid.uuid4()),
        stage_type=stage_type,
        label="Public Sale",
        price=price_wei,
        price_currency_address="0x0000000000000000000000000000000000000000",
        start_time=start.isoformat(),
        end_time=end.isoformat(),
        max_per_wallet="5",
    )


def make_raw_drop(
    chain: str = "ethereum",
    slug: str = "test-collection",
    total_supply: str = "750",
    max_supply: str = "1000",
) -> RawDrop:
    return RawDrop(
        collection_slug=slug,
        collection_name="Test Collection",
        chain=chain,
        contract_address="0xTEST123",
        drop_type="seadrop_v1_erc721",
        is_minting=False,
        opensea_url=f"https://opensea.io/collection/{slug}",
        total_supply=total_supply,
        max_supply=max_supply,
    )


@pytest.mark.asyncio
async def test_normalize_mint_eth_price():
    """Wei price → ETH → USD conversion is correct."""
    provider = OpenSeaProvider(api_key="test-key-not-used")
    stage = make_stage(price_wei="1000000000000000000")  # 1 ETH
    raw = make_raw_drop()

    mint = await provider.normalize_mint(
        raw, stage, eth_price_usd=2000.0, floor_price_usd=5000.0
    )

    assert mint.mint_price_eth == Decimal("1")
    assert mint.mint_price_usd == Decimal("2000.00")
    assert mint.offer_price_usd == Decimal("5000.00")
    await provider.close()


@pytest.mark.asyncio
async def test_normalize_mint_zero_price():
    """Zero wei price → $0 mint price."""
    provider = OpenSeaProvider(api_key="test-key-not-used")
    stage = make_stage(price_wei="0")
    raw = make_raw_drop()

    mint = await provider.normalize_mint(
        raw, stage, eth_price_usd=2000.0, floor_price_usd=10.0
    )

    assert mint.mint_price_usd == Decimal("0")
    assert mint.offer_price_usd == Decimal("10.00")
    await provider.close()


@pytest.mark.asyncio
async def test_normalize_mint_supply_calculation():
    """Minted percentage = total_supply / max_supply × 100."""
    provider = OpenSeaProvider(api_key="test-key-not-used")
    stage = make_stage()
    raw = make_raw_drop(total_supply="750", max_supply="1000")

    mint = await provider.normalize_mint(
        raw, stage, eth_price_usd=2000.0, floor_price_usd=100.0
    )

    assert mint.minted_quantity == 750
    assert mint.total_supply == 1000
    assert mint.minted_percentage == Decimal("75.00")
    await provider.close()


@pytest.mark.asyncio
async def test_normalize_mint_no_floor_price():
    """When floor_price_usd is None, offer_price_usd is None."""
    provider = OpenSeaProvider(api_key="test-key-not-used")
    stage = make_stage()
    raw = make_raw_drop()

    mint = await provider.normalize_mint(
        raw, stage, eth_price_usd=2000.0, floor_price_usd=None
    )

    assert mint.offer_price_usd is None
    await provider.close()


@pytest.mark.asyncio
async def test_normalize_mint_chain_mapping():
    """'ape_chain' API identifier maps to 'apechain' internal slug."""
    provider = OpenSeaProvider(api_key="test-key-not-used")
    stage = make_stage()
    raw = make_raw_drop(chain="ape_chain", slug="ape-collection")

    mint = await provider.normalize_mint(
        raw, stage, eth_price_usd=2000.0, floor_price_usd=50.0
    )

    assert mint.chain == "apechain"
    await provider.close()


def test_parse_dt_utc():
    """ISO 8601 timestamps are parsed to UTC-aware datetimes."""
    dt = _parse_dt("2024-01-15T18:00:00+00:00")
    assert dt.tzinfo is not None
    assert dt.hour == 18


def test_parse_dt_z_suffix():
    """Z-suffix timestamps are parsed correctly."""
    dt = _parse_dt("2024-01-15T18:00:00Z")
    assert dt.tzinfo is not None


@pytest.mark.asyncio
async def test_normalize_mint_timestamp_utc():
    """Normalised mint_start_time is timezone-aware UTC."""
    provider = OpenSeaProvider(api_key="test-key-not-used")
    stage = make_stage()
    raw = make_raw_drop()

    mint = await provider.normalize_mint(
        raw, stage, eth_price_usd=2000.0, floor_price_usd=100.0
    )

    assert mint.mint_start_time.tzinfo is not None
    await provider.close()
