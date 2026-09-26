"""
SQLAlchemy 2.x ORM models for the Public Mint Link Bot.

Tables:
  mint_opportunities  — normalised mint records
  notifications       — sent Telegram message records
  system_state        — key/value store for monitoring state

Architecture note:
  SQLite is the default; switching to PostgreSQL requires only changing
  DATABASE_URL.  All column types are compatible with both engines.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class MintOpportunityDB(Base):
    """
    Persisted mint opportunity record.

    Unique constraint on (provider, chain, contract_address, external_id)
    prevents duplicate processing even under concurrent polling cycles.
    """

    __tablename__ = "mint_opportunities"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "chain",
            "contract_address",
            "external_id",
            name="uq_mint_identity",
        ),
        Index("ix_mint_external_id", "external_id"),
        Index("ix_mint_chain", "chain"),
        Index("ix_mint_contract", "contract_address"),
        Index("ix_mint_start_time", "mint_start_time"),
        Index("ix_mint_status", "status"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False, default="opensea")
    external_id: Mapped[str] = mapped_column(String(256), nullable=False)
    collection_slug: Mapped[str] = mapped_column(String(256), nullable=False)
    project_name: Mapped[str] = mapped_column(String(512), nullable=False)
    chain: Mapped[str] = mapped_column(String(64), nullable=False)
    contract_address: Mapped[str] = mapped_column(String(256), nullable=False)
    mint_type: Mapped[str] = mapped_column(String(64), nullable=False)

    # Prices (stored as strings to preserve Decimal precision)
    mint_price_eth: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mint_price_usd: Mapped[str] = mapped_column(String(64), nullable=False)
    offer_price_usd: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Supply
    total_supply: Mapped[int | None] = mapped_column(Integer, nullable=True)
    minted_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    minted_percentage: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # Timing
    mint_start_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    mint_end_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Links
    mint_url: Mapped[str] = mapped_column(Text, nullable=False)

    # Scheduling / notification
    notification_scheduled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    notification_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    notification_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    delete_scheduled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Status
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="DISCOVERED")
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Audit
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    def __repr__(self) -> str:
        return (
            f"<MintOpportunityDB id={self.id!r} project={self.project_name!r} "
            f"chain={self.chain!r} status={self.status!r}>"
        )


class NotificationDB(Base):
    """Record of a Telegram notification that was sent."""

    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notif_mint_id", "mint_id"),
        Index("ix_notif_message_id", "message_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    mint_id: Mapped[str] = mapped_column(String(36), nullable=False)
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False)
    message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    message_thread_id: Mapped[int] = mapped_column(Integer, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    delete_scheduled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:
        return (
            f"<NotificationDB id={self.id!r} mint_id={self.mint_id!r} "
            f"message_id={self.message_id!r}>"
        )


class SystemStateDB(Base):
    """Simple key-value store for monitoring state persistence."""

    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    def __repr__(self) -> str:
        return f"<SystemStateDB key={self.key!r} value={self.value!r}>"
