"""Chain-related Pydantic models."""
from __future__ import annotations

from pydantic import BaseModel


class ChainInfo(BaseModel):
    """Serialisable representation of a chain (used in API responses and logs)."""

    name: str
    slug: str
    api_identifier: str
    chain_id: int | None
    explorer_base_url: str
    enabled: bool
    provider_supported: bool
