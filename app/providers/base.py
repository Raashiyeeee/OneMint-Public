"""
Abstract base provider and raw OpenSea drop data models.

The provider abstraction decouples the rest of the application from the
OpenSea API format.  If OpenSea changes its response structure, only this
module (and opensea.py) needs to be updated.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator, Optional

from pydantic import BaseModel

from app.models.mint import MintOpportunity


# ---------------------------------------------------------------------------
# Raw OpenSea response models (internal to provider layer)
# ---------------------------------------------------------------------------

class RawDropStage(BaseModel):
    """Maps directly to DropStageResponse in the OpenSea OpenAPI spec."""

    uuid: str
    stage_type: str
    label: Optional[str] = None
    price: Optional[str] = None                  # wei as decimal string
    price_currency_address: str
    start_time: str                              # ISO 8601
    end_time: str                                # ISO 8601
    max_per_wallet: str
    allowlist_wallet_count: Optional[int] = None

    class Config:
        extra = "allow"


class RawDrop(BaseModel):
    """Maps directly to DropResponse / DropDetailedResponse in the OpenSea OpenAPI spec."""

    collection_slug: str
    collection_name: Optional[str] = None
    chain: str
    contract_address: str
    drop_type: str
    is_minting: bool
    image_url: Optional[str] = None
    opensea_url: str
    active_stage: Optional[RawDropStage] = None
    next_stage: Optional[RawDropStage] = None
    stages: list[RawDropStage] = []
    total_supply: Optional[str] = None           # minted so far (OpenSea naming)
    max_supply: Optional[str] = None             # max tokens

    class Config:
        extra = "allow"


class RawCollectionStats(BaseModel):
    """Minimal subset of GET /api/v2/collections/{slug}/stats response."""

    floor_price: Optional[float] = None
    floor_price_symbol: Optional[str] = None

    class Config:
        extra = "allow"


# ---------------------------------------------------------------------------
# Abstract provider
# ---------------------------------------------------------------------------

class BaseMintProvider(ABC):
    """
    Abstract interface for mint data providers.

    Every method returns normalised ``MintOpportunity`` objects so the rest
    of the application never touches raw API structures.
    """

    @abstractmethod
    async def fetch_mints(
        self,
        chain_api_identifiers: list[str],
        drop_type: str = "upcoming",
    ) -> AsyncIterator[RawDrop]:
        """
        Yield raw drops from the provider for the given chains.

        Parameters
        ----------
        chain_api_identifiers:
            Provider-specific chain identifiers to filter by.
        drop_type:
            ``"upcoming"`` (default), ``"featured"``, or ``"recently_minted"``.
        """
        ...

    @abstractmethod
    async def get_mint_details(self, slug: str) -> Optional[RawDrop]:
        """
        Fetch detailed drop information including supply and all stages.

        Returns None if the drop cannot be found.
        """
        ...

    @abstractmethod
    async def get_collection_floor_price(self, slug: str) -> Optional[RawCollectionStats]:
        """
        Fetch the collection's floor-price stats.

        Returns None if unavailable.
        """
        ...

    @abstractmethod
    async def normalize_mint(
        self,
        raw: RawDrop,
        stage: RawDropStage,
        eth_price_usd: float,
        floor_price_usd: Optional[float],
    ) -> MintOpportunity:
        """
        Convert a raw drop + stage pair into a normalised MintOpportunity.

        Parameters
        ----------
        raw:
            The raw drop object.
        stage:
            The specific mint stage being normalised.
        eth_price_usd:
            Current ETH → USD conversion rate.
        floor_price_usd:
            Collection floor price in USD (used as offer proxy).
        """
        ...

    @abstractmethod
    async def close(self) -> None:
        """Release underlying HTTP session resources."""
        ...
