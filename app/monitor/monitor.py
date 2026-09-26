"""
Monitoring loop — continuously polls the OpenSea API and processes results.

Features
--------
- Asynchronous polling (non-blocking)
- Configurable interval (POLL_INTERVAL_SECONDS)
- Chain-by-chain unsupported-chain logging
- Per-record error isolation (one bad record never stops others)
- Health metric tracking
- Monitor state persisted to database (on/off control)
- Graceful shutdown via stop() + asyncio.Event
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from app.chains.registry import ChainConfig, get_chain
from app.config import Settings
from app.database.database import get_session_factory
from app.database.models import MintOpportunityDB, NotificationDB
from app.database.repository import SystemStateRepository
from app.monitor.processor import MintProcessor
from app.monitor.scheduler import MintScheduler
from app.providers.base import RawDropStage
from app.providers.opensea import OpenSeaProvider
from app.services.notification_service import TelegramNotificationService

log = logging.getLogger(__name__)

MONITOR_STATE_KEY = "monitoring_enabled"


class MonitorHealth:
    """Tracks operational metrics for the /status command."""

    def __init__(self) -> None:
        self.last_api_request: Optional[datetime] = None
        self.last_success: Optional[datetime] = None
        self.last_cycle: Optional[datetime] = None
        self.api_errors: int = 0
        self.discovered: int = 0
        self.rejected: int = 0
        self.qualified: int = 0
        self.notifications_sent: int = 0
        self.deletions: int = 0
        self.failed_ops: int = 0

    def to_dict(self) -> dict:
        return {
            "last_api_request": self.last_api_request.isoformat() if self.last_api_request else "never",
            "last_success": self.last_success.isoformat() if self.last_success else "never",
            "last_cycle": self.last_cycle.isoformat() if self.last_cycle else "never",
            "api_errors": self.api_errors,
            "discovered": self.discovered,
            "rejected": self.rejected,
            "qualified": self.qualified,
            "notifications_sent": self.notifications_sent,
            "deletions": self.deletions,
            "failed_ops": self.failed_ops,
        }


class MintMonitor:
    """
    Main monitoring loop.

    Designed to run alongside the Telegram bot in the same asyncio event loop.
    The bot remains responsive while monitoring runs in the background.
    """

    def __init__(
        self,
        settings: Settings,
        provider: OpenSeaProvider,
        scheduler: MintScheduler,
        notifier: TelegramNotificationService,
    ) -> None:
        self._settings = settings
        self._provider = provider
        self._scheduler = scheduler
        self._notifier = notifier
        self._stop_event = asyncio.Event()
        self._enabled = True
        self.health = MonitorHealth()
        self.last_eth_price: Optional[Decimal] = None

    async def start(self) -> None:
        """Start the monitoring loop. Runs until stop() is called."""
        log.info(
            "[MONITOR_CYCLE] Starting monitor | chains=%s interval=%ds",
            self._settings.enabled_chains,
            self._settings.poll_interval_seconds,
        )
        self._scheduler.start()

        # Recover any pending notification/deletion jobs after restart
        await self._recover_on_startup()

        while not self._stop_event.is_set():
            # Check if monitoring is enabled (can be toggled via /monitor command)
            if self._enabled:
                try:
                    await self._poll_cycle()
                except PermissionError as exc:
                    log.critical(
                        "[API_ERROR] Authentication failure — monitoring paused: %s", exc
                    )
                    await asyncio.sleep(60)
                except Exception as exc:
                    log.error("[MONITOR_CYCLE] Unexpected error: %s", exc, exc_info=True)
                    self.health.failed_ops += 1

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._settings.poll_interval_seconds,
                )
            except asyncio.TimeoutError:
                pass  # Normal — just continue the loop

    async def stop(self) -> None:
        """Signal the monitoring loop to stop cleanly."""
        self._stop_event.set()
        self._scheduler.shutdown()
        await self._provider.close()
        log.info("[MONITOR_CYCLE] Monitor stopped gracefully")

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        log.info("[MONITOR_CYCLE] Monitoring %s", "enabled" if enabled else "disabled")

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _poll_cycle(self) -> None:
        """Single polling cycle across all enabled chains."""
        now = datetime.now(timezone.utc)
        self.health.last_cycle = now
        self.health.last_api_request = now

        # Resolve chains
        from app.chains.registry import get_enabled_chains, get_api_identifiers

        enabled_slugs = self._settings.enabled_chain_list
        chain_configs = get_enabled_chains(enabled_slugs)

        # Log unsupported chains
        for chain in chain_configs:
            if not chain.provider_supported:
                log.info("[CHAIN_UNSUPPORTED] chain=%s", chain.slug)

        api_ids = get_api_identifiers(enabled_slugs)

        if not api_ids:
            log.warning("[MONITOR_CYCLE] No provider-supported chains enabled")
            return

        # Fetch ETH price once per cycle
        eth_price = await self._provider.get_eth_price_usd()
        self.last_eth_price = eth_price

        log.info(
            "[MONITOR_CYCLE] Polling chains=%s eth_usd=$%s",
            ",".join(api_ids),
            eth_price,
        )

        # Poll both "upcoming" and "featured" drop types
        processed_stage_ids: set[str] = set()

        for drop_type in ("upcoming", "featured"):
            try:
                async for raw_drop in self._provider.fetch_mints(api_ids, drop_type):
                    self.health.discovered += 1
                    # Process next_stage (upcoming) or active_stage (live)
                    stages_to_process = []
                    if raw_drop.next_stage:
                        stages_to_process.append(raw_drop.next_stage)
                    if raw_drop.active_stage:
                        stages_to_process.append(raw_drop.active_stage)

                    for stage in stages_to_process:
                        if stage.uuid in processed_stage_ids:
                            continue
                        processed_stage_ids.add(stage.uuid)

                        try:
                            await self._process_one(raw_drop, stage, eth_price)
                        except Exception as exc:
                            log.warning(
                                "[INVALID_DATA] external_id=%s chain=%s reason=%s",
                                stage.uuid,
                                raw_drop.chain,
                                exc,
                            )
                            self.health.failed_ops += 1

                self.health.last_success = datetime.now(timezone.utc)
            except PermissionError:
                raise
            except Exception as exc:
                log.error(
                    "[API_ERROR] drop_type=%s error=%s", drop_type, exc, exc_info=True
                )
                self.health.api_errors += 1

    async def _process_one(
        self,
        raw_drop,
        stage: RawDropStage,
        eth_price: Decimal,
    ) -> None:
        """Process a single drop+stage within its own DB transaction."""
        factory = get_session_factory()
        async with factory() as session:
            processor = MintProcessor(
                settings=self._settings,
                provider=self._provider,
                session=session,
                scheduler=self._scheduler,
                notifier=self._notifier,
                eth_price_usd=eth_price,
                health=self.health,
            )
            await processor.process(raw_drop, stage)

    async def _recover_on_startup(self) -> None:
        """
        On startup, scan the database for any SCHEDULED mints that may have
        been missed while the application was down, and reschedule them.
        """
        from app.database.models import MintOpportunityDB
        from app.database.repository import MintRepository
        from app.models.mint import MintStatus
        from app.utils.time import delete_time, notification_time
        from sqlalchemy import select

        log.info("[MONITOR_CYCLE] Running startup recovery scan...")

        factory = get_session_factory()
        async with factory() as session:
            # Recover SCHEDULED mints
            result = await session.execute(
                select(MintOpportunityDB).where(
                    MintOpportunityDB.status.in_([
                        MintStatus.SCHEDULED.value,
                        MintStatus.NOTIFIED.value,
                    ])
                )
            )
            records = list(result.scalars().all())

        for record in records:
            now = datetime.now(timezone.utc)
            mint_start = record.mint_start_time

            if mint_start.tzinfo is None:
                mint_start = mint_start.replace(tzinfo=timezone.utc)

            deletion_at = delete_time(mint_start, self._settings.delete_after_minutes)
            notif_at = notification_time(mint_start, self._settings.notification_before_minutes)

            if record.status == MintStatus.SCHEDULED.value:
                if now >= deletion_at:
                    # Too late — expire
                    async with factory() as session:
                        await MintRepository(session).update_status(
                            record.id, MintStatus.EXPIRED
                        )
                        await session.commit()
                    log.info("[MONITOR_CYCLE] Expired on recovery: %s", record.id)
                elif now >= notif_at:
                    # Should have been sent by now — send immediately
                    self._scheduler.schedule_notification(
                        mint_id=record.id,
                        run_at=now,  # immediate
                        send_fn=self._make_send_fn(record),
                    )
                else:
                    # Still in the future — reschedule
                    self._scheduler.schedule_notification(
                        mint_id=record.id,
                        run_at=notif_at,
                        send_fn=self._make_send_fn(record),
                    )
                log.info(
                    "[MONITOR_CYCLE] Recovered SCHEDULED mint: %s notify_at=%s",
                    record.id,
                    notif_at.isoformat(),
                )

            elif record.status == MintStatus.NOTIFIED.value:
                msg_id = record.notification_message_id
                if msg_id and record.delete_scheduled_at:
                    del_at = record.delete_scheduled_at
                    if del_at.tzinfo is None:
                        del_at = del_at.replace(tzinfo=timezone.utc)
                    run_at = max(del_at, now)  # if past, run immediately
                    self._scheduler.schedule_deletion(
                        mint_id=record.id,
                        message_id=msg_id,
                        run_at=run_at,
                        delete_fn=self._make_delete_fn(),
                    )
                    log.info(
                        "[MONITOR_CYCLE] Recovered NOTIFIED mint deletion: %s delete_at=%s",
                        record.id,
                        run_at.isoformat(),
                    )

        log.info(
            "[MONITOR_CYCLE] Recovery complete — recovered %d records", len(records)
        )

    def _make_send_fn(self, record: MintOpportunityDB):
        """Create a closure for the scheduler to send a notification."""
        notifier = self._notifier
        settings = self._settings

        async def _send(mint_id: str) -> None:
            from app.database.database import get_session_factory
            from app.database.models import MintOpportunityDB
            from app.database.repository import MintRepository, NotificationRepository
            from app.models.mint import MintStatus
            from app.utils.time import utcnow, delete_time
            from sqlalchemy import select
            import uuid

            factory = get_session_factory()
            async with factory() as session:
                result = await session.execute(
                    select(MintOpportunityDB).where(MintOpportunityDB.id == mint_id)
                )
                rec = result.scalar_one_or_none()
                if rec is None:
                    return

                msg_id = await notifier.send_mint_alert(rec)
                if msg_id is None:
                    await MintRepository(session).update_status(mint_id, MintStatus.FAILED)
                    await session.commit()
                    return

                self.health.notifications_sent += 1

                mint_start = rec.mint_start_time
                if mint_start.tzinfo is None:
                    mint_start = mint_start.replace(tzinfo=timezone.utc)
                del_at = delete_time(mint_start, settings.delete_after_minutes)

                notif = NotificationDB(
                    id=str(uuid.uuid4()),
                    mint_id=mint_id,
                    chat_id=settings.telegram_group_id,
                    message_id=msg_id,
                    message_thread_id=settings.public_mint_topic_id,
                    delete_scheduled_at=del_at,
                )
                await NotificationRepository(session).save_notification(notif)
                await MintRepository(session).update_status(
                    mint_id,
                    MintStatus.NOTIFIED,
                    {"notification_sent_at": utcnow(), "notification_message_id": msg_id},
                )
                await session.commit()

            self._scheduler.schedule_deletion(
                mint_id=mint_id,
                message_id=msg_id,
                run_at=del_at,
                delete_fn=self._make_delete_fn(),
            )

        return _send

    def _make_delete_fn(self):
        """Create a closure for the scheduler to delete a notification."""
        notifier = self._notifier

        async def _delete(mint_id: str, message_id: int) -> None:
            from app.database.database import get_session_factory
            from app.database.repository import MintRepository
            from app.models.mint import MintStatus
            from app.utils.time import utcnow

            deleted = await notifier.delete_mint_alert(message_id, mint_id)
            if deleted:
                self.health.deletions += 1
            factory = get_session_factory()
            async with factory() as session:
                status = MintStatus.DELETED if deleted else MintStatus.FAILED
                extra = {"deleted_at": utcnow()} if deleted else {}
                await MintRepository(session).update_status(mint_id, status, extra)
                await session.commit()

        return _delete
