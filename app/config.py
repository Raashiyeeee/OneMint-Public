"""
Configuration module for Public Mint Link Bot.
All settings are loaded from environment variables — never hard-coded.
"""
from __future__ import annotations

import os
from decimal import Decimal
from functools import lru_cache
from typing import List, Optional

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    All application settings loaded from environment variables.
    No defaults contain secrets.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ─── Telegram ─────────────────────────────────────────────────────────────
    telegram_bot_token: str = Field(..., description="Telegram Bot API token")
    telegram_group_id: int = Field(..., description="Telegram group/chat ID")
    public_mint_topic_id: int = Field(..., description="Telegram forum topic ID for public mints")
    admin_user_ids: str = Field(default="", description="Comma-separated Telegram user IDs with admin access")

    # ─── OpenSea API ──────────────────────────────────────────────────────────
    opensea_api_key: str = Field(..., description="OpenSea API key (x-api-key header)")
    opensea_api_endpoint: str = Field(
        default="https://api.opensea.io/api/v2",
        description="OpenSea API base URL",
    )

    # ─── Chain configuration ───────────────────────────────────────────────────
    monitor_all_chains: bool = Field(default=True, description="Monitor all enabled chains")
    enabled_chains: str = Field(
        default="base,ethereum,polygon,arbitrum,optimism,zora,robinhood,avalanche,bsc,shape,abstract,apechain,arc,hyperevm,ink",
        description="Comma-separated list of chain slugs to monitor",
    )

    # ─── Polling ───────────────────────────────────────────────────────────────
    poll_interval_seconds: int = Field(
        default=3600,
        ge=30,
        le=86400,
        description="Polling interval in seconds (30-86400)",
    )

    # ─── Filtering ─────────────────────────────────────────────────────────────
    min_mint_price_usd: Decimal = Field(default=Decimal("0"), description="Minimum mint price in USD")
    max_mint_price_usd: Decimal = Field(default=Decimal("20"), description="Maximum mint price in USD")
    min_offer_multiplier: Decimal = Field(default=Decimal("1.5"), description="Min offer/mint-price ratio for paid mints")
    free_mint_min_offer_usd: Decimal = Field(default=Decimal("5"), description="Min offer price for free mints")
    min_minted_percentage: Decimal = Field(default=Decimal("70"), description="Minimum minted percentage required")
    allow_sold_out: bool = Field(default=False, description="Allow 100% sold-out mints")
    duplicate_listings: bool = Field(default=False, description="Allow duplicate listings")
    # When True, mints with no floor/offer price from the API pass through with a
    # warning instead of being hard-rejected (prevents silent drops like Robinhood NFTs)
    allow_missing_offer: bool = Field(
        default=True,
        description="Pass mints through when floor/offer price is unavailable from API",
    )
    # When True, mints with no supply data from the API pass through with a warning
    allow_missing_supply: bool = Field(
        default=True,
        description="Pass mints through when minted supply data is unavailable from API",
    )

    # ─── Scheduling ────────────────────────────────────────────────────────────
    notification_before_minutes: int = Field(
        default=10, description="Minutes before mint start to send notification"
    )
    delete_after_minutes: int = Field(
        default=15, description="Minutes after mint start to delete notification"
    )

    # ─── ETH price ─────────────────────────────────────────────────────────────
    eth_price_usd: Optional[Decimal] = Field(
        default=None,
        description="Manual ETH/USD price override. If unset, fetched from CoinGecko.",
    )

    # ─── Webhook (cloud deployments) ──────────────────────────────────────────
    # Leave WEBHOOK_URL unset (or empty) to use long-polling mode (default).
    # Set WEBHOOK_URL to your public HTTPS URL (e.g. on Render) to enable
    # webhook mode — Telegram pushes updates to you instead of you polling.
    # Webhook mode is required when running more than one instance.
    webhook_url: Optional[str] = Field(
        default=None,
        description="Public HTTPS base URL for webhook mode (e.g. https://my-bot.onrender.com). Leave unset for polling.",
    )
    webhook_port: int = Field(
        default=10000,
        description="Port the aiohttp webhook server listens on (Render exposes 10000 by default).",
    )

    # ─── Operational ──────────────────────────────────────────────────────────
    test_mode: bool = Field(default=False, description="Enable test mode with mock data")
    log_level: str = Field(default="INFO", description="Python logging level")
    database_url: str = Field(
        default="sqlite+aiosqlite:///./public_mint_bot.db",
        description="SQLAlchemy async database URL",
    )
    restrict_to_admins: bool = Field(
        default=False,
        description="If true, bot silently ignores all messages from non-admins (full lockdown)",
    )

    # ─── Computed properties ──────────────────────────────────────────────────
    @property
    def admin_ids(self) -> List[int]:
        """Parse admin user IDs from comma-separated string."""
        if not self.admin_user_ids.strip():
            return []
        try:
            return [int(uid.strip()) for uid in self.admin_user_ids.split(",") if uid.strip()]
        except ValueError:
            return []

    @property
    def enabled_chain_list(self) -> List[str]:
        """Parse enabled chains from comma-separated string."""
        return [c.strip().lower() for c in self.enabled_chains.split(",") if c.strip()]

    @field_validator("poll_interval_seconds")
    @classmethod
    def validate_poll_interval(cls, v: int) -> int:
        if not (30 <= v <= 86400):
            raise ValueError("POLL_INTERVAL_SECONDS must be between 30 and 86400 (up to 24 hours)")
        return v

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in valid:
            raise ValueError(f"LOG_LEVEL must be one of {valid}")
        return upper

    @model_validator(mode="after")
    def validate_no_secrets_in_endpoint(self) -> "Settings":
        """Ensure the API endpoint does not accidentally contain the key."""
        if self.opensea_api_key and self.opensea_api_key in self.opensea_api_endpoint:
            raise ValueError("OPENSEA_API_KEY must not appear in OPENSEA_API_ENDPOINT")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Return a cached Settings instance.
    Call ``get_settings.cache_clear()`` in tests to reload from environment.
    """
    return Settings()
