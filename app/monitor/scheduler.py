"""
Persistent scheduler for notification and deletion jobs.

Uses APScheduler with a SQLAlchemy job store so that scheduled jobs survive
application restarts (Python crashes, server reboots, Docker restarts).

Job types
---------
send_notification_job(mint_id)
    Send the Telegram alert for a mint.

delete_notification_job(mint_id, message_id)
    Delete a previously sent Telegram alert.

Recovery behaviour
------------------
On restart, APScheduler replays any missed jobs immediately (misfire_grace_time).
The application also performs an active scan of the database on startup to
reschedule any jobs that may not have been committed to the APScheduler store.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler

log = logging.getLogger(__name__)

# How long past a scheduled time a job is still allowed to run (seconds)
MISFIRE_GRACE_SECONDS = 900  # 15 minutes — generous for a monitoring bot


class MintScheduler:
    """
    Wraps APScheduler to manage notification and deletion jobs.

    Parameters
    ----------
    db_url : str
        Sync SQLAlchemy URL for the APScheduler job store.
        For SQLite: ``sqlite:///./scheduler_jobs.db``
        For PostgreSQL: ``postgresql://user:pass@host/db``
    """

    def __init__(self, db_url: str) -> None:
        # APScheduler requires a sync URL; aiosqlite URLs are converted
        sync_url = db_url.replace("sqlite+aiosqlite", "sqlite")
        self._scheduler = AsyncIOScheduler(
            jobstores={
                "default": SQLAlchemyJobStore(url=sync_url),
            },
            executors={
                "default": AsyncIOExecutor(),
            },
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": MISFIRE_GRACE_SECONDS,
            },
        )

    def start(self) -> None:
        if not self._scheduler.running:
            self._scheduler.start()
            log.info("[SCHEDULER] Started APScheduler with persistent job store")

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=True)
            log.info("[SCHEDULER] Shut down APScheduler")

    def schedule_notification(
        self,
        mint_id: str,
        run_at: datetime,
        send_fn,
    ) -> Optional[str]:
        """
        Schedule a notification job.

        Parameters
        ----------
        mint_id : str
            Database mint record ID.
        run_at : datetime
            UTC time to send the notification.
        send_fn : coroutine function
            Async function to call: ``send_fn(mint_id)``

        Returns
        -------
        str or None
            APScheduler job ID, or None if scheduling failed.
        """
        job_id = f"notify_{mint_id}"
        if self._scheduler.get_job(job_id):
            log.debug("[SCHEDULER] Notification job already exists: %s", job_id)
            return job_id

        try:
            self._scheduler.add_job(
                send_fn,
                "date",
                run_date=run_at,
                args=[mint_id],
                id=job_id,
                replace_existing=True,
            )
            log.info(
                "[MINT_SCHEDULED] mint_id=%s notify_at=%s",
                mint_id,
                run_at.isoformat(),
            )
            return job_id
        except Exception as exc:
            log.error("[SCHEDULER] Failed to schedule notification: %s", exc)
            return None

    def schedule_deletion(
        self,
        mint_id: str,
        message_id: int,
        run_at: datetime,
        delete_fn,
    ) -> Optional[str]:
        """
        Schedule a deletion job for a sent Telegram message.

        Parameters
        ----------
        mint_id : str
            Database mint record ID.
        message_id : int
            Telegram message ID to delete.
        run_at : datetime
            UTC time to delete (= mint_start_time + 15 min).
        delete_fn : coroutine function
            Async function: ``delete_fn(mint_id, message_id)``
        """
        job_id = f"delete_{mint_id}"
        if self._scheduler.get_job(job_id):
            log.debug("[SCHEDULER] Deletion job already exists: %s", job_id)
            return job_id

        try:
            self._scheduler.add_job(
                delete_fn,
                "date",
                run_date=run_at,
                args=[mint_id, message_id],
                id=job_id,
                replace_existing=True,
            )
            log.info(
                "[SCHEDULER] Deletion scheduled mint_id=%s message_id=%d delete_at=%s",
                mint_id,
                message_id,
                run_at.isoformat(),
            )
            return job_id
        except Exception as exc:
            log.error("[SCHEDULER] Failed to schedule deletion: %s", exc)
            return None

    def cancel_job(self, job_id: str) -> None:
        """Cancel a scheduled job if it exists."""
        job = self._scheduler.get_job(job_id)
        if job:
            job.remove()
            log.info("[SCHEDULER] Cancelled job %s", job_id)

    def list_jobs(self) -> list[dict]:
        """Return a summary of all pending jobs."""
        return [
            {
                "id": job.id,
                "next_run_time": job.next_run_time.isoformat()
                if job.next_run_time
                else None,
            }
            for job in self._scheduler.get_jobs()
        ]
