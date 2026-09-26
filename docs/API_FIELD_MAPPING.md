# API Field Mapping

This document records the exact mapping between the Public Mint Link Bot's
internal requirements and the actual OpenSea v2 API response fields.

**Source:** `GET https://api.opensea.io/api/v2/drops` (list) and
`GET https://api.opensea.io/api/v2/drops/{slug}` (detail).
**Schema:** Verified against `https://api.opensea.io/api/v2/openapi.json`
(retrieved 2026-09-26).

---

## Endpoints Used

| Purpose                 | Method | Endpoint                              |
|-------------------------|--------|---------------------------------------|
| List upcoming drops     | GET    | `/api/v2/drops?type=upcoming`         |
| List featured drops     | GET    | `/api/v2/drops?type=featured`         |
| Drop detail + supply    | GET    | `/api/v2/drops/{slug}`                |
| Collection floor price  | GET    | `/api/v2/collections/{slug}/stats`    |
| ETH/USD price           | GET    | `https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd` |

Authentication: `x-api-key: <OPENSEA_API_KEY>` header on all OpenSea requests.

---

## Field Mapping Table

| Requirement           | API Endpoint                    | API Field Path                              | Normalized Field         | Transformation                                      | Available | Notes |
|-----------------------|---------------------------------|---------------------------------------------|--------------------------|-----------------------------------------------------|-----------|-------|
| Project name          | `/drops` or `/drops/{slug}`     | `collection_name`                           | `project_name`           | Direct string                                       | Yes       | Falls back to `collection_slug` if null |
| Collection identifier | `/drops`                        | `collection_slug`                           | `collection_slug`        | Direct string                                       | Yes       | Used as the unique key for detail/stats lookups |
| Mint type             | `/drops` → `next_stage.stage_type` | `stage_type`                             | `mint_type`              | Direct string; `"public_sale"` → public             | Yes       | See stage type values below |
| Mint price (wei)      | `/drops` → `next_stage.price`  | `price`                                     | `mint_price_eth`         | `wei_string / 1e18` → ETH (Decimal)                 | Yes       | String, may be "0" for free mints |
| Mint price (USD)      | Derived                         | Derived from `price` + ETH/USD rate         | `mint_price_usd`         | `mint_price_eth × eth_price_usd`                    | Derived   | ETH/USD from CoinGecko public API |
| **Offer price (USD)** | `/collections/{slug}/stats`     | `total.floor_price` × ETH/USD rate          | `offer_price_usd`        | `floor_price_eth × eth_price_usd`                   | **Proxy** | **See important note below** |
| Total (max) supply    | `/drops/{slug}`                 | `max_supply`                                | `total_supply`           | Integer parse                                       | Yes       | Only in detail endpoint, not list |
| Minted quantity       | `/drops/{slug}`                 | `total_supply`                              | `minted_quantity`        | Integer parse — **OpenSea uses inverted naming**    | Yes       | `total_supply` in API = currently minted count |
| Minted percentage     | Derived                         | `total_supply / max_supply × 100`           | `minted_percentage`      | Decimal, 2 d.p.                                     | Derived   | Requires both `total_supply` and `max_supply` |
| Mint start time       | `/drops` → `next_stage.start_time` | `start_time`                             | `mint_start_time`        | ISO 8601 → UTC-aware datetime                       | Yes       | |
| Mint end time         | `/drops` → `next_stage.end_time` | `end_time`                                | `mint_end_time`          | ISO 8601 → UTC-aware datetime                       | Yes       | |
| Mint URL              | `/drops`                        | `opensea_url`                               | `mint_url`               | Direct URL string                                   | Yes       | OpenSea collection drop page |
| Contract address      | `/drops`                        | `contract_address`                          | `contract_address`       | Direct hex string                                   | Yes       | |
| Chain                 | `/drops`                        | `chain`                                     | `chain`                  | Provider ID → internal slug via `CHAINS` registry   | Yes       | See chain mapping table |
| Unique mint ID        | `/drops` → `next_stage.uuid`   | `uuid`                                      | `external_id`            | Direct UUID string                                  | Yes       | Stage-level uniqueness |

---

## ⚠️ Important: Offer Price Field

### Requirement
The filtering rules require an "offer price" to evaluate against mint price.

### API Reality
The OpenSea drops endpoints (`GET /api/v2/drops` and `GET /api/v2/drops/{slug}`)
**do not provide an offer price field**.

The following were investigated and ruled out:

| Candidate Field     | Why Not Used |
|---------------------|--------------|
| `listing price`     | Not returned in drops endpoints |
| `highest offer`     | Not returned in drops endpoints; requires per-NFT lookup |
| `floor price` (listings) | Closest available proxy |

### Chosen Proxy: Collection Floor Price

**Source:** `GET /api/v2/collections/{slug}/stats`
**Field:** `total.floor_price` (ETH value)
**Conversion:** `floor_price_eth × eth_price_usd`

**What `total.floor_price` represents:**
The lowest active listing price among all NFTs in the collection.
It represents what you can buy the NFT for right now on OpenSea.

**Why this is the correct proxy:**
- It represents real market demand (what buyers are willing to pay)
- It is what you can actually sell a minted NFT for immediately after minting
- It is the most conservative (lowest) market value measure available

**Limitations:**
- Floor price reflects the cheapest listing, not the average offer
- May be 0 if no listings exist → record is skipped with `[REQUIRED_FIELD_UNAVAILABLE]`
- May fluctuate between the time a mint is detected and when it starts

**If floor_price is unavailable:**
The record is logged with `[REQUIRED_FIELD_UNAVAILABLE]` and skipped.
No alternative value is substituted.

---

## Stage Type Values (Public Mint Detection)

| OpenSea stage_type | Classification | Bot Action |
|-------------------|----------------|-----------|
| `public_sale`     | **Public**     | ✅ Proceed |
| `public`          | **Public**     | ✅ Proceed |
| `presale`         | Private/Gated  | ❌ Reject  |
| `allowlist`       | Private/Gated  | ❌ Reject  |
| `whitelist`       | Private/Gated  | ❌ Reject  |
| `fcfs`            | Private/Gated  | ❌ Reject  |
| `private_sale`    | Private        | ❌ Reject  |
| (any other)       | Unknown        | ❌ Reject  |

---

## Chain Identifier Mapping

| Display Name | Internal Slug | OpenSea API Identifier | Provider Supported | Notes |
|---|---|---|---|---|
| Base          | `base`      | `base`        | ✅ Yes | |
| Ethereum      | `ethereum`  | `ethereum`    | ✅ Yes | |
| Polygon       | `polygon`   | `polygon`     | ✅ Yes | |
| Arbitrum      | `arbitrum`  | `arbitrum`    | ✅ Yes | |
| Optimism      | `optimism`  | `optimism`    | ✅ Yes | |
| Zora          | `zora`      | `zora`        | ✅ Yes | |
| Robinhood     | `robinhood` | `robinhood`   | ✅ Yes | |
| Avalanche     | `avalanche` | `avalanche`   | ✅ Yes | |
| BSC           | `bsc`       | *(none)*      | ❌ No  | Not in OpenSea ChainIdentifier enum |
| Shape         | `shape`     | `shape`       | ✅ Yes | |
| Abstract      | `abstract`  | `abstract`    | ✅ Yes | |
| ApeChain      | `apechain`  | `ape_chain`   | ✅ Yes | Note underscore in API identifier |
| Arc           | `arc`       | `arc`         | ✅ Yes | Limited activity; may return no results |
| HyperEVM      | `hyperevm`  | `hyperevm`    | ✅ Yes | |
| Ink           | `ink`       | `ink`         | ✅ Yes | |

**Source:** `ChainIdentifier` enum from `https://api.opensea.io/api/v2/openapi.json`

---

## OpenSea Naming Inversion

> ⚠️ OpenSea uses **inverted naming** for supply fields in the detailed drop response.

| OpenSea Field Name | What It Actually Means |
|--------------------|------------------------|
| `total_supply`     | Number of tokens minted **so far** (current minted count) |
| `max_supply`       | The maximum supply cap (total tokens that can ever be minted) |

This is counter-intuitive but documented in the API schema description strings.
The bot normalizes these correctly:
- `minted_quantity` ← `api.total_supply`
- `total_supply` ← `api.max_supply`

---

## Pagination

The `GET /api/v2/drops` endpoint uses **cursor-based pagination**.

| Parameter | Type   | Description |
|-----------|--------|-------------|
| `cursor`  | string | Opaque cursor from previous response `next` field |
| `limit`   | int    | Results per page (1-100, default 20, bot uses 100) |

The bot paginates until `next` is `null`.

Note from API docs: *"May be present even when drops is empty if all items in the page were filtered by visibility rules; continue paginating until next is null."*

---

## Rate Limiting

OpenSea enforces rate limits via HTTP `429 Too Many Requests`.
The bot respects the `Retry-After` header and uses exponential backoff.

| Status | Retry? | Behavior |
|--------|--------|---------|
| 200    | No     | Parse response |
| 400    | No     | Log error, skip |
| 401    | No     | Fatal auth error |
| 403    | No     | Fatal auth error |
| 404    | No     | Skip record |
| 429    | Yes    | Honour Retry-After, then exponential backoff |
| 500    | Yes    | Exponential backoff |
| 502    | Yes    | Exponential backoff |
| 503    | Yes    | Exponential backoff |
| 504    | Yes    | Exponential backoff |
