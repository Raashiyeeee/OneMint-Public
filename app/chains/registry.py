"""
Chain registry — single source of truth for all 15 supported blockchain networks.

Display names, provider API identifiers, chain IDs, and explorer URLs are kept
separate so that a change in the upstream provider's naming never silently breaks
the application.

IMPORTANT:
  API identifiers are taken directly from the OpenSea v2 ChainIdentifier enum
  (https://api.opensea.io/api/v2/openapi.json — see schemas.ChainIdentifier).
  BSC is NOT present in that enum and is therefore marked provider_supported=False.
  Arc is in the enum as "arc" but may return no results at query time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass(frozen=True)
class ChainConfig:
    """
    Immutable configuration for a single blockchain network.

    Attributes
    ----------
    name : str
        Human-readable display name used in Telegram messages.
    slug : str
        Internal slug (lower-case, used as dict key and in .env lists).
    api_identifier : str
        The exact string the OpenSea API expects for this chain.
        Empty string → chain is not supported by the provider.
    chain_id : Optional[int]
        EVM chain ID (None for chains where it is unknown or irrelevant).
    explorer_base_url : str
        Block-explorer URL prefix for contract addresses, e.g.
        ``https://etherscan.io/address/``.
    enabled : bool
        Whether this chain is enabled for monitoring by default.
    provider_supported : bool
        False when the upstream API does not support this chain.
        A warning is logged at startup and the chain is skipped during polling.
    """

    name: str
    slug: str
    api_identifier: str
    chain_id: Optional[int]
    explorer_base_url: str
    enabled: bool = True
    provider_supported: bool = True


# ---------------------------------------------------------------------------
# Chain registry
# All 15 chains required by the project specification.
# API identifiers verified against the OpenSea v2 ChainIdentifier enum.
# ---------------------------------------------------------------------------

CHAINS: Dict[str, ChainConfig] = {
    "base": ChainConfig(
        name="Base",
        slug="base",
        api_identifier="base",           # enum: "base"
        chain_id=8453,
        explorer_base_url="https://basescan.org/address/",
    ),
    "ethereum": ChainConfig(
        name="Ethereum",
        slug="ethereum",
        api_identifier="ethereum",       # enum: "ethereum"
        chain_id=1,
        explorer_base_url="https://etherscan.io/address/",
    ),
    "polygon": ChainConfig(
        name="Polygon",
        slug="polygon",
        api_identifier="polygon",        # enum: "polygon"
        chain_id=137,
        explorer_base_url="https://polygonscan.com/address/",
    ),
    "arbitrum": ChainConfig(
        name="Arbitrum",
        slug="arbitrum",
        api_identifier="arbitrum",       # enum: "arbitrum"
        chain_id=42161,
        explorer_base_url="https://arbiscan.io/address/",
    ),
    "optimism": ChainConfig(
        name="Optimism",
        slug="optimism",
        api_identifier="optimism",       # enum: "optimism"
        chain_id=10,
        explorer_base_url="https://optimistic.etherscan.io/address/",
    ),
    "zora": ChainConfig(
        name="Zora",
        slug="zora",
        api_identifier="zora",           # enum: "zora"
        chain_id=7777777,
        explorer_base_url="https://explorer.zora.energy/address/",
    ),
    "robinhood": ChainConfig(
        name="Robinhood",
        slug="robinhood",
        api_identifier="robinhood",      # enum: "robinhood"
        chain_id=None,                   # Chain ID not publicly documented
        explorer_base_url="https://explorer.robinhood.com/address/",
    ),
    "avalanche": ChainConfig(
        name="Avalanche",
        slug="avalanche",
        api_identifier="avalanche",      # enum: "avalanche"
        chain_id=43114,
        explorer_base_url="https://snowtrace.io/address/",
    ),
    "bsc": ChainConfig(
        name="BSC",
        slug="bsc",
        api_identifier="",               # NOT in OpenSea ChainIdentifier enum
        chain_id=56,
        explorer_base_url="https://bscscan.com/address/",
        enabled=True,
        provider_supported=False,        # OpenSea does not support BSC
    ),
    "shape": ChainConfig(
        name="Shape",
        slug="shape",
        api_identifier="shape",          # enum: "shape"
        chain_id=360,
        explorer_base_url="https://shapescan.xyz/address/",
    ),
    "abstract": ChainConfig(
        name="Abstract",
        slug="abstract",
        api_identifier="abstract",       # enum: "abstract"
        chain_id=2741,
        explorer_base_url="https://abscan.org/address/",
    ),
    "apechain": ChainConfig(
        name="ApeChain",
        slug="apechain",
        api_identifier="ape_chain",      # enum: "ape_chain"  ← note underscore
        chain_id=33139,
        explorer_base_url="https://apescan.io/address/",
    ),
    "arc": ChainConfig(
        name="Arc",
        slug="arc",
        api_identifier="arc",            # enum: "arc"
        chain_id=None,                   # Chain ID not publicly documented
        explorer_base_url="",            # Explorer not publicly documented
    ),
    "hyperevm": ChainConfig(
        name="HyperEVM",
        slug="hyperevm",
        api_identifier="hyperevm",       # enum: "hyperevm"
        chain_id=999,
        explorer_base_url="https://explorer.hyperliquid.xyz/address/",
    ),
    "ink": ChainConfig(
        name="Ink",
        slug="ink",
        api_identifier="ink",            # enum: "ink"
        chain_id=57073,
        explorer_base_url="https://explorer.inkonchain.com/address/",
    ),
}


def get_chain(slug: str) -> Optional[ChainConfig]:
    """Return chain config by slug (case-insensitive). None if not found."""
    return CHAINS.get(slug.lower())


def get_chain_by_api_id(api_identifier: str) -> Optional[ChainConfig]:
    """Return chain config by its upstream API identifier. None if not found."""
    for chain in CHAINS.values():
        if chain.api_identifier == api_identifier:
            return chain
    return None


def get_enabled_chains(enabled_slugs: list[str]) -> list[ChainConfig]:
    """
    Return the list of ChainConfig objects that are both:
    - present in enabled_slugs
    - enabled=True
    - provider_supported=True  (unsupported chains are logged separately)

    Unsupported chains in enabled_slugs still appear in the returned list so
    that the monitor can log [CHAIN_UNSUPPORTED] for them.
    """
    result = []
    for slug in enabled_slugs:
        chain = get_chain(slug)
        if chain is None:
            continue  # Unknown slug — ignored silently
        if chain.enabled:
            result.append(chain)
    return result


def get_api_identifiers(enabled_slugs: list[str]) -> list[str]:
    """
    Return only the API identifiers for chains that are both enabled and
    provider-supported. Used to build the ?chains= query parameter.
    """
    return [
        c.api_identifier
        for c in get_enabled_chains(enabled_slugs)
        if c.provider_supported and c.api_identifier
    ]
