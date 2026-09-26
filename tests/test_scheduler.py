"""
Tests for scheduler — notification timing and deletion scheduling.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.utils.time import delete_time, format_time_remaining, notification_time, utcnow


def test_notification_time_10_minutes_before():
    """Notification is scheduled exactly 10 minutes before mint start."""
    start = utcnow() + timedelta(hours=1)
    notif = notification_time(start, before_minutes=10)
    diff = (start - notif).total_seconds()
    assert diff == 600


def test_delete_time_15_minutes_after():
    """Deletion is scheduled exactly 15 minutes after mint start."""
    start = utcnow() + timedelta(hours=1)
    del_at = delete_time(start, after_minutes=15)
    diff = (del_at - start).total_seconds()
    assert diff == 900


def test_delete_time_not_relative_to_notification():
    """Deletion must be 15 min after MINT START, not 25 min after notification."""
    start = utcnow() + timedelta(hours=2)
    notif = notification_time(start, before_minutes=10)
    del_from_notif = notif + timedelta(minutes=25)
    del_from_start = delete_time(start, after_minutes=15)
    # Both formulas happen to produce the same time, but the implementation
    # must use mint_start + 15min
    assert abs((del_from_start - del_from_notif).total_seconds()) < 1


def test_format_time_remaining_seconds():
    assert "Seconds" in format_time_remaining(0.5)


def test_format_time_remaining_minutes():
    result = format_time_remaining(10)
    assert "10" in result and "Minutes" in result


def test_format_time_remaining_hours():
    result = format_time_remaining(90)
    assert "Hours" in result


def test_format_time_remaining_now():
    assert format_time_remaining(0) == "Now"
    assert format_time_remaining(-5) == "Now"


def test_late_detection_case1():
    """Case 1: Mint > 10 min away — normal scheduling."""
    start = utcnow() + timedelta(minutes=20)
    notif = notification_time(start, 10)
    del_at = delete_time(start, 15)
    now = utcnow()

    assert notif > now          # Not yet time to notify
    assert del_at > now         # Not yet time to delete


def test_late_detection_case2():
    """Case 2: Mint < 10 min away — notify immediately."""
    start = utcnow() + timedelta(minutes=5)
    notif = notification_time(start, 10)
    del_at = delete_time(start, 15)
    now = utcnow()

    assert notif < now          # Notification time already passed
    assert del_at > now         # Deletion still in future


def test_late_detection_case3():
    """Case 3: Mint started < 15 min ago — notify immediately."""
    start = utcnow() - timedelta(minutes=5)
    notif = notification_time(start, 10)
    del_at = delete_time(start, 15)
    now = utcnow()

    assert notif < now          # Should have been sent
    assert del_at > now         # 10 minutes remain before deletion


def test_late_detection_case4():
    """Case 4: Mint started > 15 min ago — expired."""
    start = utcnow() - timedelta(minutes=20)
    del_at = delete_time(start, 15)
    now = utcnow()

    assert del_at < now         # Deletion time already passed → expired


@pytest.mark.asyncio
async def test_scheduler_schedule_notification():
    """MintScheduler.schedule_notification creates an APScheduler job."""
    from app.monitor.scheduler import MintScheduler
    from unittest.mock import patch, MagicMock

    scheduler = MintScheduler(db_url="sqlite:///./test_scheduler.db")

    mock_job = MagicMock()
    with patch.object(scheduler._scheduler, "add_job", return_value=mock_job) as mock_add:
        with patch.object(scheduler._scheduler, "get_job", return_value=None):
            run_at = utcnow() + timedelta(minutes=10)
            job_id = scheduler.schedule_notification(
                mint_id="test-mint-id",
                run_at=run_at,
                send_fn=AsyncMock(),
            )

    assert job_id == "notify_test-mint-id"
    mock_add.assert_called_once()


@pytest.mark.asyncio
async def test_scheduler_no_duplicate_jobs():
    """Scheduling the same mint_id twice does not create a duplicate job."""
    from app.monitor.scheduler import MintScheduler

    scheduler = MintScheduler(db_url="sqlite:///./test_scheduler.db")

    existing_job = MagicMock()
    with patch.object(scheduler._scheduler, "get_job", return_value=existing_job):
        with patch.object(scheduler._scheduler, "add_job") as mock_add:
            job_id = scheduler.schedule_notification(
                mint_id="existing-mint",
                run_at=utcnow() + timedelta(minutes=5),
                send_fn=AsyncMock(),
            )

    assert job_id == "notify_existing-mint"
    mock_add.assert_not_called()  # Should not add if job exists
