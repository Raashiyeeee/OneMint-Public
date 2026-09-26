"""
Normalised internal models for the Public Mint Link Bot.

All monetary values use Decimal to avoid floating-point rounding errors.
All datetime objects are timezone-aware UTC.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class MintStatus(str, Enum):
    """
    Valid status transitions:

    DISCOVERED → VALIDATED → SCHEDULED → NOTIFIED → DELETE_PENDING → DELETED
    DISCOVERED → VALIDATED → REJECTED
    DISCOVERED → VALIDATED → EXPIRED
    Any state  → FAILED
    """

    DISCOVERED = "DISCOVERED"
    VALIDATED = "VALIDATED"
    REJECTED = "REJECTED"
    SCHEDULED = "SCHEDULED"
    NOTIFIED = "NOTIFIED"
    DELETE_PENDING = "DELETE_PENDING"
    DELETED = "DELETED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


class MintOpportunity(BaseModel):
    """
    Normalised representation of a single NFT mint opportunity.

    All fields map from the OpenSea API through the provider normalisation
    layer.  Monetary values are Decimal; times are UTC-aware datetimes.
    """

    # Identity
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    provider: str = Field(default="opensea", description="Data provider name")
    external_id: str = Field(..., description="Provider-specific unique identifier (stage UUID)")

    # Collection info
    project_name: str = Field(..., description="Human-readable collection/project name")
    chain: str = Field(..., description="Internal chain slug (e.g. 'base', 'ethereum')")
    contract_address: str = Field(..., description="NFT contract address")

    # Mint details
    mint_type: str = Field(..., description="Mint stage type (e.g. 'public_sale')")
    mint_price_eth: Optional[Decimal] = Field(
        None, description="Mint price in native ETH/chain token"
    )
    mint_price_usd: Decimal = Field(..., description="Mint price in USD")
    offer_price_usd: Optional[Decimal] = Field(
        None,
        description=(
            "Best collection offer (floor-level) in USD. "
            "Sourced from GET /api/v2/collections/{slug}/stats → total.floor_price. "
            "See docs/API_FIELD_MAPPING.md for the full rationale."
        ),
    )

    # Supply
    total_supply: Optional[int] = Field(None, description="Maximum supply (max_supply from API)")
    minted_quantity: Optional[int] = Field(None, description="Current minted count (total_supply from API)")
    minted_percentage: Optional[Decimal] = Field(None, description="Minted percentage 0-100")

    # Timing
    mint_start_time: datetime = Field(..., description="Stage start time (UTC)")
    mint_end_time: Optional[datetime] = Field(None, description="Stage end time (UTC)")

    # Links
    mint_url: str = Field(..., description="OpenSea URL for the drop")
    collection_slug: str = Field(..., description="OpenSea collection slug")

    # Tracking
    detected_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When this mint was discovered",
    )
    notification_scheduled_at: Optional[datetime] = None
    notification_sent_at: Optional[datetime] = None
    notification_message_id: Optional[int] = None
    delete_scheduled_at: Optional[datetime] = None
    deleted_at: Optional[datetime] = None
    status: MintStatus = MintStatus.DISCOVERED

    # Timestamps
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("mint_start_time", "mint_end_time", mode="before")
    @classmethod
    def ensure_utc(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is None:
            return v
        if isinstance(v, datetime) and v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v

    @field_validator("minted_percentage", mode="before")
    @classmethod
    def validate_percentage(cls, v: Optional[Decimal]) -> Optional[Decimal]:
        if v is None:
            return v
        v = Decimal(str(v))
        if v < 0 or v > 100:
            raise ValueError(f"minted_percentage must be 0-100, got {v}")
        return v

    @field_validator("mint_price_usd", mode="before")
    @classmethod
    def validate_price_non_negative(cls, v: Decimal) -> Decimal:
        v = Decimal(str(v))
        if v < 0:
            raise ValueError(f"mint_price_usd must be >= 0, got {v}")
        return v

    class Config:
        use_enum_values = True
