"""
OpenSea v2 API provider implementation.

Endpoints used
--------------
GET /api/v2/drops
    List upcoming/featured/recently_minted drops.
    Auth: x-api-key header.
    Pagination: cursor-based (?cursor=<next>).

GET /api/v2/drops/{slug}
    Detailed drop with all stages, total_supply, max_supply.
    Auth: x-api-key header.

GET /api/v2/collections/{slug}/stats
    Collection floor price (used as the "offer" metric).
    Auth: x-api-key header.

GET https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd
    ETH/USD price — public endpoint, no auth required.

API field mapping
-----------------
See docs/API_FIELD_MAPPING.md for a complete mapping table.

IMPORTANT — "Offer" field
--------------------------
The OpenSea drops endpoints do NOT expose an "offer price" field.
The closest available proxy is the *collection floor price* from the
/stats endpoint (total.floor_price in ETH).  This is documented explicitly
and is the value used for offer-price filter evaluation.

If floor_price is unavailable the record is skipped with [REQUIRED_FIELD_UNAVAILABLE].
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import AsyncIterator, Optional

import aiohttp

from app.chains.registry import get_chain_by_api_id
from app.models.mint import MintOpportunity, MintStatus
from app.providers.base import (
    BaseMintProvider,
    RawCollectionStats,
    RawDrop,
    RawDropStage,
)
from app.utils.retry import RetryConfig, with_retry

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
NATIVE_TOKEN_ADDRESS = "0x0000000000000000000000000000000000000000"
PUBLIC_STAGE_TYPES = {"public_sale", "public"}
COINGECKO_ETH_URL = (
    "https://api.coingecko.com/api/v3/simple/price"
    "?ids=ethereum&vs_currencies=usd"
)
DEFAULT_ETH_USD = Decimal("2000")   # Fallback if CoinGecko is unreachable


class OpenSeaProvider(BaseMintProvider):
    """
    Concrete provider for the OpenSea v2 REST API.

    Parameters
    ----------
    api_key : str
        Passed as the ``x-api-key`` header on every request.
        Never logged.
    base_url : str
        Base URL for the API, e.g. ``https://api.opensea.io/api/v2``.
    retry_config : RetryConfig
        Governs exponential-backoff retry behaviour.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.opensea.io/api/v2",
        retry_config: Optional[RetryConfig] = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._retry = retry_config or RetryConfig()
        self._session: Optional[aiohttp.ClientSession] = None

    # ── HTTP session ──────────────────────────────────────────────────────────

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "x-api-key": self._api_key,
                    "Accept": "application/json",
                    "User-Agent": "PublicMintLinkBot/1.0",
                },
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ── Raw HTTP helpers ──────────────────────────────────────────────────────

    async def _get(self, url: str, params: Optional[dict] = None) -> dict:
        """
        Perform a GET request with retry logic.
        Raises aiohttp.ClientError on unrecoverable failure.
        """
        async def _do_get() -> dict:
            session = self._get_session()
            log.debug("[API_REQUEST] url=%s params=%s", url, params)
            async with session.get(url, params=params) as resp:
                if resp.status == 429:
                    retry_after = int(resp.headers.get("Retry-After", "5"))
                    log.warning(
                        "[API_ERROR] status=429 retry_after=%d url=%s",
                        retry_after,
                        url,
                    )
                    await asyncio.sleep(retry_after)
                    raise aiohttp.ClientResponseError(
                        resp.request_info,
                        resp.history,
                        status=429,
                        message="Rate limited",
                    )
                if resp.status in (500, 502, 503, 504):
                    log.warning("[API_ERROR] status=%d url=%s", resp.status, url)
                    resp.raise_for_status()
                if resp.status == 401 or resp.status == 403:
                    log.error(
                        "[API_ERROR] Authentication failure status=%d — "
                        "check OPENSEA_API_KEY",
                        resp.status,
                    )
                    raise PermissionError(
                        f"OpenSea API authentication error: HTTP {resp.status}"
                    )
                resp.raise_for_status()
                data = await resp.json()
                log.debug("[API_SUCCESS] url=%s", url)
                return data

        return await with_retry(_do_get, self._retry)

    # ── Public provider methods ───────────────────────────────────────────────

    async def fetch_mints(
        self,
        chain_api_identifiers: list[str],
        drop_type: str = "upcoming",
    ) -> AsyncIterator[RawDrop]:
        """
        Yield raw drops from GET /api/v2/drops using cursor pagination.

        Yields all pages for the requested drop_type and chain filter.
        """
        url = f"{self._base_url}/drops"
        cursor: Optional[str] = None
        page = 0

        while True:
            params: dict = {
                "type": drop_type,
                "limit": 100,
            }
            if chain_api_identifiers:
                params["chains"] = ",".join(chain_api_identifiers)
            if cursor:
                params["cursor"] = cursor

            page += 1
            log.info(
                "[API_REQUEST] GET /drops page=%d type=%s chains=%s",
                page,
                drop_type,
                ",".join(chain_api_identifiers) if chain_api_identifiers else "all",
            )

            try:
                data = await self._get(url, params)
            except PermissionError:
                raise  # Auth errors bubble up immediately — do not retry in monitor
            except Exception as exc:
                log.error("[API_ERROR] fetch_mints failed: %s", exc)
                return

            drops_raw = data.get("drops", [])
            for raw_dict in drops_raw:
                try:
                    yield RawDrop(**raw_dict)
                except Exception as exc:
                    log.warning(
                        "[INVALID_DATA] Failed to parse drop: %s | data=%s",
                        exc,
                        raw_dict.get("collection_slug", "?"),
                    )

            cursor = data.get("next")
            if not cursor:
                break

    async def get_mint_details(self, slug: str) -> Optional[RawDrop]:
        """Fetch detailed drop information from GET /api/v2/drops/{slug}."""
        url = f"{self._base_url}/drops/{slug}"
        try:
            data = await self._get(url)
            return RawDrop(**data)
        except aiohttp.ClientResponseError as exc:
            if exc.status == 404:
                log.warning("[API_ERROR] Drop not found: slug=%s", slug)
                return None
            log.error("[API_ERROR] get_mint_details slug=%s error=%s", slug, exc)
            return None
        except Exception as exc:
            log.error("[API_ERROR] get_mint_details slug=%s error=%s", slug, exc)
            return None

    async def get_collection_floor_price(self, slug: str) -> Optional[RawCollectionStats]:
        """
        Fetch the collection's floor price from GET /api/v2/collections/{slug}/stats.

        The "total.floor_price" field is the cheapest active listing price in ETH.
        This is the proxy used for the required "offer" metric.

        See docs/API_FIELD_MAPPING.md — Offer/Floor price section.
        """
        url = f"{self._base_url}/collections/{slug}/stats"
        try:
            data = await self._get(url)
            total = data.get("total", {})
            floor_price = total.get("floor_price")
            floor_price_symbol = total.get("floor_price_symbol", "ETH")
            return RawCollectionStats(
                floor_price=float(floor_price) if floor_price is not None else None,
                floor_price_symbol=floor_price_symbol,
            )
        except aiohttp.ClientResponseError as exc:
            if exc.status == 404:
                return None
            log.warning(
                "[API_ERROR] get_collection_floor_price slug=%s error=%s", slug, exc
            )
            return None
        except Exception as exc:
            log.warning(
                "[API_ERROR] get_collection_floor_price slug=%s error=%s", slug, exc
            )
            return None

    async def get_eth_price_usd(self) -> Decimal:
        """
        Fetch the current ETH/USD price from CoinGecko (public, no auth).
        Falls back to DEFAULT_ETH_USD on failure.
        """
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(COINGECKO_ETH_URL) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    price = data["ethereum"]["usd"]
                    return Decimal(str(price))
        except Exception as exc:
            log.warning(
                "[API_ERROR] CoinGecko ETH price fetch failed: %s — using fallback $%s",
                exc,
                DEFAULT_ETH_USD,
            )
            return DEFAULT_ETH_USD

    # ── Normalisation ─────────────────────────────────────────────────────────

    async def normalize_mint(
        self,
        raw: RawDrop,
        stage: RawDropStage,
        eth_price_usd: float,
        floor_price_usd: Optional[float],
    ) -> MintOpportunity:
        """
        Convert a raw (drop, stage) pair into a normalised MintOpportunity.

        Monetary conversions
        --------------------
        - Mint price: stage.price (wei) → ETH → USD using eth_price_usd.
        - Offer price: floor_price_usd (already USD).

        Timestamps
        ----------
        stage.start_time / end_time are ISO 8601 strings → parsed to UTC datetime.

        Chain
        -----
        raw.chain (OpenSea API identifier) → internal slug via registry.
        """
        eth_usd = Decimal(str(eth_price_usd))

        # ── Mint price ──────────────────────────────────────────────────────
        mint_price_eth = Decimal("0")
        mint_price_usd = Decimal("0")
        if stage.price:
            try:
                wei = Decimal(stage.price)
                mint_price_eth = (wei / Decimal("1e18")).quantize(
                    Decimal("0.000001"), rounding=ROUND_HALF_UP
                )
                mint_price_usd = (mint_price_eth * eth_usd).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            except InvalidOperation:
                pass  # stays zero — validator will reject if needed

        # ── Offer / floor price ─────────────────────────────────────────────
        offer_price_usd: Optional[Decimal] = None
        if floor_price_usd is not None:
            offer_price_usd = Decimal(str(floor_price_usd)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )

        # ── Chain mapping ───────────────────────────────────────────────────
        chain_config = get_chain_by_api_id(raw.chain)
        chain_slug = chain_config.slug if chain_config else raw.chain

        # ── Timestamps ──────────────────────────────────────────────────────
        mint_start = _parse_dt(stage.start_time)
        mint_end = _parse_dt(stage.end_time)

        # ── Supply ──────────────────────────────────────────────────────────
        # OpenSea naming is inverted in the detailed response:
        #   total_supply = how many have been minted so far
        #   max_supply   = the hard cap
        minted_quantity: Optional[int] = None
        total_supply: Optional[int] = None
        minted_percentage: Optional[Decimal] = None

        if raw.total_supply is not None:
            try:
                minted_quantity = int(raw.total_supply)
            except (ValueError, TypeError):
                pass

        if raw.max_supply is not None:
            try:
                total_supply = int(raw.max_supply)
            except (ValueError, TypeError):
                pass

        if minted_quantity is not None and total_supply and total_supply > 0:
            pct = Decimal(minted_quantity) / Decimal(total_supply) * 100
            minted_percentage = pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        return MintOpportunity(
            provider="opensea",
            external_id=stage.uuid,
            project_name=raw.collection_name or raw.collection_slug,
            chain=chain_slug,
            contract_address=raw.contract_address,
            mint_type=stage.stage_type,
            mint_price_eth=mint_price_eth,
            mint_price_usd=mint_price_usd,
            offer_price_usd=offer_price_usd,
            total_supply=total_supply,
            minted_quantity=minted_quantity,
            minted_percentage=minted_percentage,
            mint_start_time=mint_start,
            mint_end_time=mint_end,
            mint_url=raw.opensea_url,
            collection_slug=raw.collection_slug,
            status=MintStatus.DISCOVERED,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_dt(iso_str: str) -> datetime:
    """Parse an ISO 8601 string to a UTC-aware datetime."""
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
