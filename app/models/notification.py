"""Notification model."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field


class NotificationRecord(BaseModel):
    """Persisted record of a sent Telegram notification."""

    id: str
    mint_id: str
    chat_id: int
    message_id: int
    message_thread_id: int
    sent_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    delete_scheduled_at: Optional[datetime] = None
    deleted_at: Optional[datetime] = None
