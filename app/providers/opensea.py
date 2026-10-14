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
COINBASE_ETH_URL = "https://api.coinbase.com/v2/prices/ETH-USD/spot"
BINANCE_ETH_URL = "https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT"
DEFAULT_ETH_USD = Decimal("2000")   # Fallback if all sources are unreachable


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
        self._cached_eth_price: Optional[Decimal] = None
        self._cached_eth_time: Optional[float] = None

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

    async def get_collection_offer_price(
        self,
        slug: str,
        chain: Optional[str] = None,
        contract_address: Optional[str] = None,
    ) -> Optional[float]:
        """
        Fetch the collection's highest active offer price in USD from OpenSea v2 API.
        Endpoint: GET /api/v2/offers/collection/{slug}/all
        """
        import time

        resolved_slug = slug
        url = f"{self._base_url}/offers/collection/{resolved_slug}/all"
        data = None

        try:
            data = await self._get(url, params={"limit": 20})
        except aiohttp.ClientResponseError as exc:
            if exc.status == 404 and chain and contract_address:
                # Attempt to resolve collection slug from contract address
                try:
                    contract_url = f"{self._base_url}/chain/{chain}/contract/{contract_address}"
                    contract_data = await self._get(contract_url)
                    resolved_slug = contract_data.get("collection")
                    if resolved_slug:
                        url = f"{self._base_url}/offers/collection/{resolved_slug}/all"
                        data = await self._get(url, params={"limit": 20})
                except Exception as c_exc:
                    log.debug(
                        "[API_ERROR] Could not resolve collection for CA %s: %s",
                        contract_address,
                        c_exc,
                    )
            else:
                log.warning(
                    "[API_ERROR] get_collection_offer_price slug=%s error=%s", slug, exc
                )
                return None
        except Exception as exc:
            log.warning(
                "[API_ERROR] get_collection_offer_price slug=%s error=%s", slug, exc
            )
            return None

        if not data:
            return None

        offers = data.get("offers", [])
        if not offers:
            log.debug("[OFFERS] No offers found for collection slug=%s", resolved_slug)
            return None

        now_ts = time.time()
        best_offer_usd: Optional[Decimal] = None
        eth_price: Optional[Decimal] = None

        for offer in offers:
            status = offer.get("status")
            if status and str(status).upper() != "ACTIVE":
                continue

            # Verify that the offer is specifically for the drop's contract address (CA)
            if contract_address and not _offer_matches_contract(offer, contract_address):
                continue

            params = offer.get("protocol_data", {}).get("parameters", {})
            end_time = params.get("endTime")
            if end_time:
                try:
                    if float(end_time) < now_ts:
                        continue
                except (ValueError, TypeError):
                    pass

            price_info = offer.get("price") or {}
            val_str = price_info.get("value")
            if not val_str:
                continue

            try:
                val_int = Decimal(str(val_str))
                decimals = int(price_info.get("decimals") or 18)
                token_amount = val_int / (Decimal(10) ** decimals)
            except Exception:
                continue

            currency = (price_info.get("currency") or "WETH").upper()
            if currency in ("ETH", "WETH"):
                if eth_price is None:
                    eth_price = await self.get_reliable_eth_price_usd()
                if eth_price is None:
                    continue  # no live ETH/USD rate -> not a credible USD value
                offer_usd = token_amount * eth_price
            elif currency in ("USDC", "USDT", "DAI", "USD", "USDBC"):
                offer_usd = token_amount
            else:
                # Unknown currency: we cannot convert it credibly, so skip it
                # rather than pretending it is ETH.
                log.debug("[OFFERS] Skipping offer in unsupported currency %s", currency)
                continue

            if best_offer_usd is None or offer_usd > best_offer_usd:
                best_offer_usd = offer_usd

        if best_offer_usd is not None:
            quantized = best_offer_usd.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            log.info(
                "[OFFER_FOUND] Collection %s best active offer: $%s",
                resolved_slug,
                quantized,
            )
            return float(quantized)

        return None

    async def get_reliable_eth_price_usd(self) -> Optional[Decimal]:
        """Return a live (or <= 5 min cached) ETH/USD rate, or None.

        Unlike get_eth_price_usd() this never returns the hard-coded
        DEFAULT_ETH_USD fallback or a stale cache, because USD thresholds
        ($13 cap, 1.40x offer) must not be evaluated on a made-up rate.
        """
        import time

        price = await self.get_eth_price_usd()
        if (
            self._cached_eth_price is not None
            and self._cached_eth_time is not None
            and time.time() - self._cached_eth_time < 300
        ):
            return price
        return None

    async def get_eth_price_usd(self) -> Decimal:
        """
        Fetch the current ETH/USD price with multi-source fallback and 5-minute caching.
        Sources in order:
          1. In-memory cache (< 5 minutes old)
          2. CoinGecko
          3. Coinbase
          4. Binance
          5. Previous cached price or DEFAULT_ETH_USD
        """
        import time

        now = time.time()
        if self._cached_eth_price and self._cached_eth_time and (now - self._cached_eth_time < 300):
            return self._cached_eth_price

        timeout = aiohttp.ClientTimeout(total=5)

        # 1. CoinGecko
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(COINGECKO_ETH_URL) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        price = Decimal(str(data["ethereum"]["usd"]))
                        self._cached_eth_price = price
                        self._cached_eth_time = now
                        return price
        except Exception:
            pass

        # 2. Coinbase fallback
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(COINBASE_ETH_URL) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        price = Decimal(str(data["data"]["amount"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                        self._cached_eth_price = price
                        self._cached_eth_time = now
                        return price
        except Exception:
            pass

        # 3. Binance fallback
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(BINANCE_ETH_URL) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        price = Decimal(str(data["price"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                        self._cached_eth_price = price
                        self._cached_eth_time = now
                        return price
        except Exception:
            pass

        if self._cached_eth_price:
            return self._cached_eth_price

        log.warning("[API_ERROR] All ETH price sources failed — using fallback $%s", DEFAULT_ETH_USD)
        return DEFAULT_ETH_USD

    # ── Normalisation ─────────────────────────────────────────────────────────

    async def normalize_mint(
        self,
        raw: RawDrop,
        stage: RawDropStage,
        eth_price_usd: float,
        floor_price_usd: Optional[float] = None,
        offer_price_usd: Optional[float] = None,
    ) -> MintOpportunity:
        """
        Convert a raw (drop, stage) pair into a normalised MintOpportunity.

        Monetary conversions
        --------------------
        - Mint price: stage.price (wei) → ETH → USD using eth_price_usd.
        - Offer price: offer_price_usd or floor_price_usd (already USD).

        Timestamps
        ----------
        stage.start_time / end_time are ISO 8601 strings → parsed to UTC datetime.

        Chain
        -----
        raw.chain (OpenSea API identifier) → internal slug via registry.
        """
        from app.rules import ETH_NATIVE_CHAINS

        eth_usd = Decimal(str(eth_price_usd)) if eth_price_usd is not None else None
        chain_config = get_chain_by_api_id(raw.chain)
        chain_slug = chain_config.slug if chain_config else raw.chain

        # ── Mint price ──────────────────────────────────────────────────────
        # Never default an unknown price to $0 (that would make it look free).
        mint_price_eth: Optional[Decimal] = None
        mint_price_usd = Decimal("0")
        price_unreliable: Optional[str] = None
        currency = (stage.price_currency_address or "").lower()
        if stage.price is None or str(stage.price).strip() == "":
            price_unreliable = "stage price missing"
        elif currency and currency != NATIVE_TOKEN_ADDRESS:
            price_unreliable = f"price currency {currency} is not the native token"
        elif chain_slug not in ETH_NATIVE_CHAINS:
            price_unreliable = f"native token of chain '{chain_slug}' is not ETH"
        else:
            try:
                wei = Decimal(str(stage.price))
                if wei < 0:
                    raise InvalidOperation
                mint_price_eth = wei / Decimal("1e18")
                if wei == 0:
                    mint_price_usd = Decimal("0")
                elif eth_usd is None or eth_usd <= 0:
                    price_unreliable = "no live ETH/USD rate"
                else:
                    # Keep sub-cent precision so $13.004 is not rounded to $13.00.
                    mint_price_usd = (mint_price_eth * eth_usd).quantize(Decimal("0.00000001"))
            except InvalidOperation:
                price_unreliable = f"unparseable stage price {stage.price!r}"

        # ── Offer price ─────────────────────────────────────────────────────
        target_offer = offer_price_usd if offer_price_usd is not None else floor_price_usd
        final_offer_usd: Optional[Decimal] = None
        if target_offer is not None:
            final_offer_usd = Decimal(str(target_offer)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )

        # ── Timestamps (UTC) ────────────────────────────────────────────────
        mint_start = _parse_dt(stage.start_time)
        start_unreliable: Optional[str] = None
        mint_end: Optional[datetime] = None
        try:
            mint_end = _parse_dt(stage.end_time) if stage.end_time else None
        except ValueError:
            start_unreliable = "stage end time unparseable"
        if mint_start.year < 2000:
            start_unreliable = "start time is a placeholder epoch value"

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
            contract_address=raw.contract_address.lower(),
            mint_type=stage.stage_type,
            stage_label=stage.label,
            allowlist_wallet_count=stage.allowlist_wallet_count,
            price_unreliable_reason=price_unreliable,
            start_time_unreliable_reason=start_unreliable,
            mint_price_eth=mint_price_eth,
            mint_price_usd=mint_price_usd,
            offer_price_usd=final_offer_usd,
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
    return dt.astimezone(timezone.utc)


def _offer_matches_contract(offer: dict, target_contract: Optional[str]) -> bool:
    """
    Check if an OpenSea offer is specifically for target_contract (address).

    If target_contract is None, returns True.
    """
    if not target_contract:
        return True

    target_ca = target_contract.lower().strip()

    # 1. Direct asset contract check
    asset = offer.get("asset")
    if isinstance(asset, dict):
        c = asset.get("contract")
        if c:
            return str(c).lower() == target_ca

    # 2. Criteria contract check (collection/trait offers)
    criteria = offer.get("criteria")
    if isinstance(criteria, dict):
        c_obj = criteria.get("contract")
        if isinstance(c_obj, dict):
            c_addr = c_obj.get("address")
            if c_addr:
                return str(c_addr).lower() == target_ca

    # 3. Protocol consideration items (Seaport protocol parameters)
    # itemType 2 (ERC721), 3 (ERC1155), 4 (ERC721_WITH_CRITERIA), 5 (ERC1155_WITH_CRITERIA)
    params = offer.get("protocol_data", {}).get("parameters", {})
    found_nft_item = False
    for item in params.get("consideration", []):
        if item.get("itemType") in (2, 3, 4, 5):
            found_nft_item = True
            token = str(item.get("token", "")).lower()
            if token == target_ca:
                return True

    if found_nft_item:
        return False

    return True
