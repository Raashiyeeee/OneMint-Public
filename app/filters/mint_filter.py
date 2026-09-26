"""
Mint filtering engine.

Implements all five filtering rules from the specification:

Rule 1 — PUBLIC MINT
    stage_type must be "public_sale" or "public".

Rule 2 — MINT PRICE
    0 <= mint_price_usd <= 20.

Rule 3 — NORMAL MINT (price > 0)
    offer_price_usd >= mint_price_usd × 1.5

Rule 4 — FREE MINT (price == 0)
    offer_price_usd >= 5

Rule 5 — MINTED SUPPLY
    minted_percentage >= MIN_MINTED_PERCENTAGE (default 70)
    AND (if ALLOW_SOLD_OUT=false) minted_percentage < 100
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
        Rule 3 (paid mint): offer >= price × 1.5
        Rule 4 (free mint): offer >= 5
        """
        if mint.offer_price_usd is None:
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
            # Rule 3: normal mint
            required = (price * self._cfg.min_offer_multiplier).quantize(Decimal("0.01"))
            if offer < required:
                return FilterResult(
                    passed=False,
                    rule="RULE3_OFFER_RATIO",
                    reason=(
                        f"offer_price_usd={offer} < required={required} "
                        f"(mint_price_usd={price} × {self._cfg.min_offer_multiplier})"
                    ),
                )
        else:
            # Rule 4: free mint
            if offer < self._cfg.free_mint_min_offer_usd:
                return FilterResult(
                    passed=False,
                    rule="RULE4_FREE_MINT_OFFER",
                    reason=(
                        f"offer_price_usd={offer} < min={self._cfg.free_mint_min_offer_usd} "
                        "for free mint"
                    ),
                )
        return None

    def _rule5_minted(self, mint: MintOpportunity) -> Optional[FilterResult]:
        """Rule 5: minted_percentage must be in the configured range."""
        if mint.minted_percentage is None:
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
        if pct < self._cfg.min_minted_percentage:
            return FilterResult(
                passed=False,
                rule="RULE5_MINTED",
                reason=f"minted_percentage={pct}% < min={self._cfg.min_minted_percentage}%",
            )

        if pct >= Decimal("100") and not self._cfg.allow_sold_out:
            return FilterResult(
                passed=False,
                rule="RULE5_SOLD_OUT",
                reason=f"minted_percentage={pct}% (sold out) and ALLOW_SOLD_OUT=false",
            )

        return None
