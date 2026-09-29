"""
Mint filtering engine.

Implements all five filtering rules from the specification:

Rule 1 — PUBLIC MINT
    stage_type must be "public_sale" or "public".

Rule 2 — MINT PRICE
    min_mint_price_usd <= mint_price_usd <= max_mint_price_usd.
    The max is a hard upper bound (exact); all values at or below pass.

Rule 3 — NORMAL MINT (price > 0)
    offer_price_usd >= mint_price_usd × min_offer_multiplier
    i.e. offer must be AT LEAST the user-set multiplier — higher is better and
    always passes.  If offer_price_usd is unavailable and allow_missing_offer
    is True the mint is passed through with a warning.

Rule 4 — FREE MINT (price == 0)
    offer_price_usd >= free_mint_min_offer_usd
    If offer_price_usd is unavailable and allow_missing_offer is True the mint
    is passed through with a warning.

Rule 5 — MINTED SUPPLY
    minted_percentage >= min_minted_percentage (threshold is the MINIMUM;
    higher values always pass).  If supply data is unavailable and
    allow_missing_supply is True the mint is passed through with a warning.
    AND (if ALLOW_SOLD_OUT=false) minted_percentage < 100

Comparison semantics summary
-----------------------------
  max_mint_price_usd   — exact upper bound (reject if price > max)
  min_offer_multiplier — minimum threshold (pass if offer >= price × threshold)
  free_mint_min_offer  — minimum threshold (pass if offer >= threshold)
  min_minted_percentage— minimum threshold (pass if pct >= threshold)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from app.models.mint import MintOpportunity

log = logging.getLogger(__name__)


@dataclass
class FilterConfig:
    min_mint_price_usd: Decimal = Decimal("0")
    max_mint_price_usd: Decimal = Decimal("20")
    min_offer_multiplier: Decimal = Decimal("1.5")
    free_mint_min_offer_usd: Decimal = Decimal("5")
    min_minted_percentage: Decimal = Decimal("70")
    allow_sold_out: bool = False
    # When True, mints whose floor/offer price is unavailable from the API are
    # passed through with a warning instead of being hard-rejected.
    allow_missing_offer: bool = True
    # When True, mints whose supply data is unavailable from the API are
    # passed through with a warning instead of being hard-rejected.
    allow_missing_supply: bool = True


@dataclass
class FilterResult:
    passed: bool
    rule: str
    reason: str


PUBLIC_STAGE_TYPES = {"public_sale", "public"}


class MintFilter:
    """Stateless filter — apply all rules to a MintOpportunity."""

    def __init__(self, config: FilterConfig) -> None:
        self._cfg = config

    def evaluate(self, mint: MintOpportunity) -> FilterResult:
        """
        Evaluate all filtering rules in order.

        Returns the first failing rule, or a passing result if all pass.
        Each rule method returns a FilterResult on failure, or None on pass.
        """
        for check in (
            self._rule1_public,
            self._rule2_price_range,
            self._rule3_or_4_offer,
            self._rule5_minted,
        ):
            result = check(mint)
            if result is not None:
                return result
        return FilterResult(passed=True, rule="ALL_PASS", reason="All rules passed")

    # ── Individual rule evaluators ────────────────────────────────────────────

    def _rule1_public(self, mint: MintOpportunity) -> Optional[FilterResult]:
        """Rule 1: stage_type must unambiguously identify a public mint."""
        if mint.mint_type.lower() not in PUBLIC_STAGE_TYPES:
            return FilterResult(
                passed=False,
                rule="RULE1_PUBLIC",
                reason=f"mint_type='{mint.mint_type}' is not a public stage",
            )
        return None

    def _rule2_price_range(self, mint: MintOpportunity) -> Optional[FilterResult]:
        """Rule 2: 0 <= mint_price_usd <= 20."""
        price = mint.mint_price_usd
        if price < self._cfg.min_mint_price_usd:
            return FilterResult(
                passed=False,
                rule="RULE2_PRICE",
                reason=f"mint_price_usd={price} < min={self._cfg.min_mint_price_usd}",
            )
        if price > self._cfg.max_mint_price_usd:
            return FilterResult(
                passed=False,
                rule="RULE2_PRICE",
                reason=f"mint_price_usd={price} > max={self._cfg.max_mint_price_usd}",
            )
        return None

    def _rule3_or_4_offer(self, mint: MintOpportunity) -> Optional[FilterResult]:
        """
        Rule 3 (paid mint):  offer_price_usd >= mint_price_usd × min_offer_multiplier
        Rule 4 (free mint):  offer_price_usd >= free_mint_min_offer_usd

        Comparison is >= the configured threshold; any value equal to OR above
        the threshold passes.  A value below the threshold rejects.

        If offer_price_usd is None (floor price not available from API):
          - allow_missing_offer=True  → pass through with a WARNING
          - allow_missing_offer=False → hard reject
        """
        if mint.offer_price_usd is None:
            if self._cfg.allow_missing_offer:
                log.warning(
                    "[FILTER_WARN] external_id=%s chain=%s "
                    "field=offer_price_usd reason=no_floor_price_available "
                    "action=passing_through (allow_missing_offer=True)",
                    mint.external_id,
                    mint.chain,
                )
                return None  # pass through
            log.warning(
                "[REQUIRED_FIELD_UNAVAILABLE] external_id=%s chain=%s "
                "field=offer_price_usd reason=no_floor_price_available",
                mint.external_id,
                mint.chain,
            )
            return FilterResult(
                passed=False,
                rule="RULE3_4_OFFER",
                reason="offer_price_usd unavailable (no floor price from API)",
            )

        offer = mint.offer_price_usd
        price = mint.mint_price_usd

        if price > Decimal("0"):
            # Rule 3: normal/paid mint
            # Pass if offer >= price × min_offer_multiplier  (>= means equal or higher also passes)
            required = (price * self._cfg.min_offer_multiplier).quantize(Decimal("0.01"))
            actual_multiplier = (offer / price).quantize(Decimal("0.001")) if price else Decimal("0")
            if offer >= required:
                log.debug(
                    "[FILTER_PASS] RULE3 external_id=%s offer=$%s >= required=$%s "
                    "(%.3fx >= %.3fx threshold)",
                    mint.external_id,
                    offer,
                    required,
                    actual_multiplier,
                    self._cfg.min_offer_multiplier,
                )
                return None
            return FilterResult(
                passed=False,
                rule="RULE3_OFFER_RATIO",
                reason=(
                    f"offer_price_usd={offer} < required={required} "
                    f"(mint_price_usd={price} × {self._cfg.min_offer_multiplier}x threshold; "
                    f"actual={actual_multiplier}x)"
                ),
            )
        else:
            # Rule 4: free mint — pass if offer >= threshold (equal or higher also passes)
            if offer >= self._cfg.free_mint_min_offer_usd:
                log.debug(
                    "[FILTER_PASS] RULE4 external_id=%s offer=$%s >= free_mint_min=$%s",
                    mint.external_id,
                    offer,
                    self._cfg.free_mint_min_offer_usd,
                )
                return None
            return FilterResult(
                passed=False,
                rule="RULE4_FREE_MINT_OFFER",
                reason=(
                    f"offer_price_usd={offer} < min={self._cfg.free_mint_min_offer_usd} "
                    "for free mint"
                ),
            )

    def _rule5_minted(self, mint: MintOpportunity) -> Optional[FilterResult]:
        """
        Rule 5: minted_percentage must be >= min_minted_percentage.

        Comparison is >= the configured threshold; any value equal to OR above
        the threshold passes.  A value below the threshold rejects.

        If minted_percentage is None (supply data unavailable from API):
          - allow_missing_supply=True  → pass through with a WARNING
          - allow_missing_supply=False → hard reject
        """
        if mint.minted_percentage is None:
            if self._cfg.allow_missing_supply:
                log.warning(
                    "[FILTER_WARN] external_id=%s chain=%s "
                    "field=minted_percentage reason=supply_data_unavailable "
                    "action=passing_through (allow_missing_supply=True)",
                    mint.external_id,
                    mint.chain,
                )
                return None  # pass through
            log.warning(
                "[REQUIRED_FIELD_UNAVAILABLE] external_id=%s chain=%s "
                "field=minted_percentage reason=supply_data_unavailable",
                mint.external_id,
                mint.chain,
            )
            return FilterResult(
                passed=False,
                rule="RULE5_MINTED",
                reason="minted_percentage unavailable (supply data missing from API)",
            )

        pct = mint.minted_percentage
        # Pass if pct >= threshold (equal to OR above the threshold both pass)
        if pct >= self._cfg.min_minted_percentage:
            log.debug(
                "[FILTER_PASS] RULE5 external_id=%s minted=%s%% >= threshold=%s%%",
                mint.external_id,
                pct,
                self._cfg.min_minted_percentage,
            )
        else:
            return FilterResult(
                passed=False,
                rule="RULE5_MINTED",
                reason=(
                    f"minted_percentage={pct}% < min={self._cfg.min_minted_percentage}% "
                    f"(must be >= threshold to pass)"
                ),
            )

        if pct >= Decimal("100") and not self._cfg.allow_sold_out:
            return FilterResult(
                passed=False,
                rule="RULE5_SOLD_OUT",
                reason=f"minted_percentage={pct}% (sold out) and ALLOW_SOLD_OUT=false",
            )

        return None
