"""
Health service — system-wide operational metrics.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


class HealthService:
    """Thread-safe health metric store."""

    def __init__(self) -> None:
        self.start_time: datetime = datetime.now(timezone.utc)
        self.last_api_request: Optional[datetime] = None
        self.last_success: Optional[datetime] = None
        self.last_cycle: Optional[datetime] = None
        self.api_error_count: int = 0
        self.discovered_count: int = 0
        self.qualified_count: int = 0
        self.rejected_count: int = 0
        self.notification_count: int = 0
        self.deletion_count: int = 0
        self.failed_count: int = 0

    def record_api_request(self) -> None:
        self.last_api_request = datetime.now(timezone.utc)

    def record_success(self) -> None:
        self.last_success = datetime.now(timezone.utc)

    def record_cycle(self) -> None:
        self.last_cycle = datetime.now(timezone.utc)

    def to_dict(self) -> dict:
        return {
            "uptime_seconds": (datetime.now(timezone.utc) - self.start_time).total_seconds(),
            "last_api_request": self.last_api_request.isoformat() if self.last_api_request else None,
            "last_success": self.last_success.isoformat() if self.last_success else None,
            "last_cycle": self.last_cycle.isoformat() if self.last_cycle else None,
            "api_errors": self.api_error_count,
            "discovered": self.discovered_count,
            "qualified": self.qualified_count,
            "rejected": self.rejected_count,
            "notifications": self.notification_count,
            "deletions": self.deletion_count,
            "failed": self.failed_count,
        }
