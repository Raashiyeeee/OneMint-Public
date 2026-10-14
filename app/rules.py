"""
Spec constants — the single source of truth for the alerting rules.

These values come straight from the product spec and are NOT runtime-tunable
in a way that could loosen them.  Settings may only make the price/offer/supply
rules *stricter* (see app.config validators); timing is fixed.

1. Eligibility: public mint only, price <= $13, >= 70% minted, reliable start
   time, credible offer price.
2. Offer >= mint price x 1.40 (gross, before fees).
3. One alert at mint_start - 15 min, re-checked right before sending.
4. Delete at mint_start + 10 min.
5. No duplicates, UTC everywhere, reschedule on start-time change, persist and
   recover pending jobs.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

MAX_MINT_PRICE_USD = Decimal("13")
MIN_OFFER_MULTIPLIER = Decimal("1.40")
MIN_MINTED_PERCENTAGE = Decimal("70")

ALERT_BEFORE_START = timedelta(minutes=15)
DELETE_AFTER_START = timedelta(minutes=10)

# A send job that fires later than this after mint_start - 15m (e.g. the
# process was down) is considered missed and is NOT sent late.
SEND_GRACE = timedelta(seconds=60)

# Start times further out than this are treated as placeholders, not confirmed.
MAX_START_HORIZON = timedelta(days=180)

# Chains whose native gas/mint token is ETH. Prices quoted in the native token
# on other chains (POL, AVAX, BNB, APE, HYPE, ...) cannot be converted with the
# ETH/USD rate, so the mint price is treated as unreliable there.
ETH_NATIVE_CHAINS = {
    "ethereum", "base", "arbitrum", "optimism", "zora", "shape", "abstract",
    "ink", "robinhood",
}
