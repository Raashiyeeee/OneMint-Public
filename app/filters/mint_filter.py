"""
Mint eligibility engine — implements spec rules 1 and 2.

Rule 1 — Eligibility (every check is a hard requirement, nothing passes on
missing data):
    1a  public mint only: stage_type is public AND the stage is not labelled
        or configured as a whitelist / allowlist / presale stage.
    1b  mint price is known/reliable and <= $13 USD.
    1c  >= 70% of total supply already minted (supply data required).
    1d  public mint start time is confirmed and reliable: timezone-aware,
        in the future, before the stage end, and within a sane horizon.
    1e  a credible offer price exists (> 0, from a live, contract-matched offer).

Rule 2 — Offer ratio:
    offer_price_usd >= mint_price_usd x 1.40, compared gross (no fees) and
    exactly (no rounding of the required threshold).

Settings may make these thresholds stricter but never looser; see
FilterConfig.__post_init__.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

from app import rules
from app.models.mint import MintOpportunity
from app.utils.time import ensure_utc, utcnow

log = logging.getLogger(__name__)

PUBLIC_STAGE_TYPES = {"public_sale", "public"}

# Any of these words in a stage label/type means it is NOT a public phase.
_NON_PUBLIC_RE = re.compile(
    r"(white\s*-?list|allow\s*-?list|\bwl\b|pre\s*-?sale|presale|private|"
    r"\bgtd\b|guaranteed|\bfcfs\b|\bog\b)",
    re.IGNORECASE,
)

# Rules whose outcome can change between discovery and mint start
# (minted %, offer, USD price). Failing these at discovery still schedules a
# re-check at alert time; failing a static rule does not.
DYNAMIC_RULES = {"RULE1B_PRICE", "RULE1C_MINTED", "RULE1E_OFFER", "RULE2_OFFER_RATIO"}


@dataclass
class FilterConfig:
    max_mint_price_usd: Decimal = rules.MAX_MINT_PRICE_USD
    min_offer_multiplier: Decimal = rules.MIN_OFFER_MULTIPLIER
    min_minted_percentage: Decimal = rules.MIN_MINTED_PERCENTAGE
    allow_sold_out: bool = False

    def __post_init__(self) -> None:
        # Clamp to the spec: config can tighten, never loosen.
        self.max_mint_price_usd = min(Decimal(self.max_mint_price_usd), rules.MAX_MINT_PRICE_USD)
        self.min_offer_multiplier = max(Decimal(self.min_offer_multiplier), rules.MIN_OFFER_MULTIPLIER)
        self.min_minted_percentage = max(Decimal(self.min_minted_percentage), rules.MIN_MINTED_PERCENTAGE)


@dataclass
class FilterResult:
    passed: bool
    rule: str
    reason: str

    @property
    def is_dynamic_failure(self) -> bool:
        return (not self.passed) and self.rule in DYNAMIC_RULES


class MintFilter:
    """Stateless filter — apply all rules to a MintOpportunity."""

    def __init__(self, config: Optional[FilterConfig] = None) -> None:
        self._cfg = config or FilterConfig()

    def evaluate(self, mint: MintOpportunity, now: Optional[datetime] = None) -> FilterResult:
        now = ensure_utc(now) or utcnow()
        for check in (
            self._rule1a_public,
            lambda m: self._rule1d_start_time(m, now),
            self._rule1b_price,
            self._rule1c_minted,
            self._rule1e_offer,
            self._rule2_offer_ratio,
        ):
            result = check(mint)
            if result is not None:
                return result
        return FilterResult(passed=True, rule="ALL_PASS", reason="All rules passed")

    # ── Rule 1a ───────────────────────────────────────────────────────────────
    def _rule1a_public(self, mint: MintOpportunity) -> Optional[FilterResult]:
        stage_type = (mint.mint_type or "").strip().lower()
        if stage_type not in PUBLIC_STAGE_TYPES:
            return _fail("RULE1A_PUBLIC", f"mint_type='{mint.mint_type}' is not a public stage")
        label = mint.stage_label or ""
        if _NON_PUBLIC_RE.search(label):
            return _fail("RULE1A_PUBLIC", f"stage label '{label}' indicates a whitelist/allowlist stage")
        if mint.allowlist_wallet_count:
            return _fail(
                "RULE1A_PUBLIC",
                f"stage has allowlist_wallet_count={mint.allowlist_wallet_count} (gated stage)",
            )
        return None

    # ── Rule 1b ───────────────────────────────────────────────────────────────
    def _rule1b_price(self, mint: MintOpportunity) -> Optional[FilterResult]:
        if mint.price_unreliable_reason:
            return _fail("RULE1B_PRICE", f"mint price unreliable: {mint.price_unreliable_reason}")
        price = mint.mint_price_usd
        if price is None or price < 0:
            return _fail("RULE1B_PRICE", "mint price unavailable")
        if price > self._cfg.max_mint_price_usd:
            return _fail("RULE1B_PRICE", f"mint_price_usd={price} > max={self._cfg.max_mint_price_usd}")
        return None

    # ── Rule 1c ───────────────────────────────────────────────────────────────
    def _rule1c_minted(self, mint: MintOpportunity) -> Optional[FilterResult]:
        pct = mint.minted_percentage
        if pct is None:
            return _fail("RULE1C_MINTED", "minted_percentage unavailable (supply data missing)")
        if pct < self._cfg.min_minted_percentage:
            return _fail("RULE1C_MINTED", f"minted_percentage={pct}% < min={self._cfg.min_minted_percentage}%")
        if pct >= Decimal("100") and not self._cfg.allow_sold_out:
            return _fail("RULE1C_SOLD_OUT", f"minted_percentage={pct}% (sold out)")
        return None

    # ── Rule 1d ───────────────────────────────────────────────────────────────
    def _rule1d_start_time(self, mint: MintOpportunity, now: datetime) -> Optional[FilterResult]:
        start = mint.mint_start_time
        if start is None:
            return _fail("RULE1D_START_TIME", "start time missing")
        if start.tzinfo is None:
            return _fail("RULE1D_START_TIME", "start time is not timezone-aware")
        if mint.start_time_unreliable_reason:
            return _fail("RULE1D_START_TIME", mint.start_time_unreliable_reason)
        if start <= now:
            return _fail("RULE1D_START_TIME", f"start time {start.isoformat()} is not in the future")
        if start - now > rules.MAX_START_HORIZON:
            return _fail("RULE1D_START_TIME", f"start time {start.isoformat()} is beyond the confirmed horizon")
        end = mint.mint_end_time
        if end is not None and end <= start:
            return _fail("RULE1D_START_TIME", "stage end time is not after start time")
        return None

    # ── Rule 1e ───────────────────────────────────────────────────────────────
    def _rule1e_offer(self, mint: MintOpportunity) -> Optional[FilterResult]:
        offer = mint.offer_price_usd
        if offer is None:
            return _fail("RULE1E_OFFER", "no credible offer price available")
        if offer <= 0:
            return _fail("RULE1E_OFFER", f"offer_price_usd={offer} is not a credible offer")
        return None

    # ── Rule 2 ────────────────────────────────────────────────────────────────
    def _rule2_offer_ratio(self, mint: MintOpportunity) -> Optional[FilterResult]:
        offer = mint.offer_price_usd
        price = mint.mint_price_usd
        required = price * self._cfg.min_offer_multiplier  # exact, gross, no rounding
        if offer >= required:
            return None
        return _fail(
            "RULE2_OFFER_RATIO",
            f"offer_price_usd={offer} < required={required} "
            f"(mint_price_usd={price} x {self._cfg.min_offer_multiplier})",
        )


def _fail(rule: str, reason: str) -> FilterResult:
    return FilterResult(passed=False, rule=rule, reason=reason)
