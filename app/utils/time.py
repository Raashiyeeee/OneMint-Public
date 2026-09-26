"""Time utility helpers. All operations use UTC exclusively."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional


def utcnow() -> datetime:
    """Return the current UTC time (timezone-aware)."""
    return datetime.now(timezone.utc)


def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Convert a naive datetime to UTC-aware, or return None."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def minutes_until(dt: datetime) -> float:
    """Return signed minutes until ``dt`` from now (negative = past)."""
    return (dt - utcnow()).total_seconds() / 60.0


def notification_time(mint_start: datetime, before_minutes: int = 10) -> datetime:
    """Return the time the notification should be sent."""
    return mint_start - timedelta(minutes=before_minutes)


def delete_time(mint_start: datetime, after_minutes: int = 15) -> datetime:
    """Return the time the notification should be deleted."""
    return mint_start + timedelta(minutes=after_minutes)


def format_time_remaining(minutes: float) -> str:
    """Format a minute count as a human-readable string."""
    if minutes <= 0:
        return "Now"
    if minutes < 1:
        secs = int(minutes * 60)
        return f"{secs} Seconds"
    if minutes < 60:
        return f"{int(minutes)} Minutes"
    hours = minutes / 60
    return f"{hours:.1f} Hours"
