"""
Mint processor — orchestrates the full pipeline for a single mint opportunity.

Pipeline
--------
raw API data → normalize → validate → filter → deduplicate → save → schedule

Late-detection logic
--------------------
Case 1: mint_start > now + 10 min  → schedule normally
Case 2: now < mint_start <= now + 10 min → send immediately
Case 3: mint_start <= now < mint_start + 15 min → send immediately
Case 4: now >= mint_start + 15 min → mark EXPIRED
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from app.monitor.monitor import MonitorHealth

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import MintOpportunityDB, NotificationDB
from app.database.repository import MintRepository, NotificationRepository
from app.filters.mint_filter import FilterConfig, MintFilter
from app.models.mint import MintOpportunity, MintStatus
from app.monitor.scheduler import MintScheduler
from app.providers.base import RawDrop, RawDropStage
from app.providers.opensea import OpenSeaProvider
from app.services.notification_service import TelegramNotificationService
from app.utils.time import delete_time, notification_time, utcnow

log = logging.getLogger(__name__)


class MintProcessor:
    """
    Processes a single (drop, stage) pair through the full pipeline.

    Parameters
    ----------
    settings : Settings
        Application configuration.
    provider : OpenSeaProvider
        API provider (already initialised).
    session : AsyncSession
        Active database session (caller manages transaction).
    scheduler : MintScheduler
        APScheduler wrapper for persistent jobs.
    notifier : TelegramNotificationService
        Telegram bot wrapper.
    eth_price_usd : Decimal
        Current ETH/USD price (fetched once per polling cycle).
    """

    def __init__(
        self,
        settings: Settings,
        provider: OpenSeaProvider,
        session: AsyncSession,
        scheduler: MintScheduler,
        notifier: TelegramNotificationService,
        eth_price_usd: Decimal,
        health: Optional[MonitorHealth] = None,
    ) -> None:
        self._settings = settings
        self._provider = provider
        self._session = session
        self._scheduler = scheduler
        self._notifier = notifier
        self._eth_price_usd = eth_price_usd
        self._health = health

        self._filter = MintFilter(
            FilterConfig(
                min_mint_price_usd=settings.min_mint_price_usd,
                max_mint_price_usd=settings.max_mint_price_usd,
                min_offer_multiplier=settings.min_offer_multiplier,
                free_mint_min_offer_usd=settings.free_mint_min_offer_usd,
                min_minted_percentage=settings.min_minted_percentage,
                allow_sold_out=settings.allow_sold_out,
                allow_missing_offer=settings.allow_missing_offer,
                allow_missing_supply=settings.allow_missing_supply,
            )
        )

    async def process(self, raw: RawDrop, stage: RawDropStage) -> None:
        """Process a single (drop, stage) pair through the full pipeline."""
        mint_repo = MintRepository(self._session)
        notif_repo = NotificationRepository(self._session)

        # ── Step 1: Validate raw data ─────────────────────────────────────────
        if not _validate_raw(raw, stage):
            return  # [INVALID_DATA] already logged inside

        # ── Step 2: Fetch supply and floor price ──────────────────────────────
        detailed = await self._provider.get_mint_details(raw.collection_slug)
        floor_stats = await self._provider.get_collection_floor_price(raw.collection_slug)
        floor_price_usd: Optional[float] = None
        if floor_stats and floor_stats.floor_price is not None:
            floor_price_usd = float(self._eth_price_usd) * floor_stats.floor_price

        # Use detailed supply if available.
        # NOTE: Use explicit `is not None` — the `or` operator would treat 0 as
        # falsy and silently fall back to the list value, breaking minted_percentage
        # for new drops that have 0 tokens minted so far.
        if detailed:
            if detailed.total_supply is not None:
                raw.total_supply = detailed.total_supply
            if detailed.max_supply is not None:
                raw.max_supply = detailed.max_supply

        # ── Step 3: Normalise ─────────────────────────────────────────────────
        try:
            mint = await self._provider.normalize_mint(
                raw, stage, float(self._eth_price_usd), floor_price_usd
            )
        except Exception as exc:
            log.warning(
                "[INVALID_DATA] external_id=%s chain=%s field=normalise reason=%s",
                stage.uuid,
                raw.chain,
                exc,
            )
            return

        # ── Step 4: Filter ────────────────────────────────────────────────────
        result = self._filter.evaluate(mint)
        if not result.passed:
            log.info(
                "[MINT_REJECTED] external_id=%s chain=%s rule=%s reason=%s",
                mint.external_id,
                mint.chain,
                result.rule,
                result.reason,
            )
            if self._health:
                self._health.rejected += 1
            mint.status = MintStatus.REJECTED
            # Save rejected records for auditing (non-blocking)
            try:
                db_record, is_new = await mint_repo.save_mint(mint)
                if is_new:
                    await mint_repo.update_status(
                        db_record.id,
                        MintStatus.REJECTED,
                        {"rejection_reason": result.reason},
                    )
                    await self._session.commit()
            except Exception:
                await self._session.rollback()
            return

        log.info(
            "[MINT_QUALIFIED] external_id=%s project=%r chain=%s price=$%s offer=$%s minted=%s%%",
            mint.external_id,
            mint.project_name,
            mint.chain,
            mint.mint_price_usd,
            mint.offer_price_usd,
            mint.minted_percentage,
        )
        if self._health:
            self._health.qualified += 1

        # ── Step 5: Deduplicate and save ──────────────────────────────────────
        db_record, is_new = await mint_repo.save_mint(mint)
        if not is_new:
            log.info(
                "[MINT_DUPLICATE] external_id=%s chain=%s",
                mint.external_id,
                mint.chain,
            )
            await self._session.rollback()
            return

        # ── Step 6: Determine timing ──────────────────────────────────────────
        now = utcnow()
        mint_start = mint.mint_start_time
        notification_at = notification_time(mint_start, self._settings.notification_before_minutes)
        deletion_at = delete_time(mint_start, self._settings.delete_after_minutes)

        # ── Step 7: Late-detection cases ──────────────────────────────────────
        if now >= deletion_at:
            # Case 4: mint is expired
            log.info(
                "[MINT_EXPIRED] external_id=%s mint_started=%s mins_ago=%.1f",
                mint.external_id,
                mint_start.isoformat(),
                (now - mint_start).total_seconds() / 60,
            )
            await mint_repo.update_status(db_record.id, MintStatus.EXPIRED)
            await self._session.commit()
            return

        # ── Step 8: Schedule or send ──────────────────────────────────────────
        mint_status = MintStatus.SCHEDULED

        if now >= notification_at:
            # Cases 2 & 3: send immediately
            await self._session.commit()  # Save record first
            await self._send_now(db_record, deletion_at, notif_repo)
        else:
            # Case 1: schedule for later
            await mint_repo.update_status(
                db_record.id,
                MintStatus.SCHEDULED,
                {
                    "notification_scheduled_at": notification_at,
                    "delete_scheduled_at": deletion_at,
                },
            )
            await self._session.commit()

            self._scheduler.schedule_notification(
                mint_id=db_record.id,
                run_at=notification_at,
                send_fn=self._send_scheduled,
            )

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _get_all_broadcast_targets(self, session: AsyncSession) -> list[tuple[int, Optional[int]]]:
        """Fetch all active destinations: the primary .env target + any registered in DB."""
        from app.database.repository import TargetRepository
        targets: list[tuple[int, Optional[int]]] = [
            (self._settings.telegram_group_id, self._settings.public_mint_topic_id)
        ]
        seen = {(self._settings.telegram_group_id, self._settings.public_mint_topic_id)}
        try:
            extra = await TargetRepository(session).get_all_active()
            for t in extra:
                key = (t.chat_id, t.topic_id)
                if key not in seen:
                    seen.add(key)
                    targets.append(key)
        except Exception as exc:
            log.warning("[BROADCAST] Could not load extra targets from DB: %s", exc)
        return targets

    async def _send_now(
        self,
        db_record: MintOpportunityDB,
        deletion_at: datetime,
        notif_repo: NotificationRepository,
    ) -> None:
        """Send the notification immediately to all broadcast targets (late detection cases 2 & 3)."""
        targets = await self._get_all_broadcast_targets(self._session)
        sent_messages: list[tuple[int, int, Optional[int]]] = []

        for chat_id, topic_id in targets:
            msg_id = await self._notifier.send_to_target(db_record, chat_id, topic_id)
            if msg_id:
                sent_messages.append((chat_id, msg_id, topic_id))

        if not sent_messages:
            await MintRepository(self._session).update_status(
                db_record.id, MintStatus.FAILED
            )
            await self._session.commit()
            return

        if self._health:
            self._health.notifications_sent += len(sent_messages)

        primary_msg_id = sent_messages[0][1]
        for chat_id, msg_id, topic_id in sent_messages:
            notif = NotificationDB(
                id=str(uuid.uuid4()),
                mint_id=db_record.id,
                chat_id=chat_id,
                message_id=msg_id,
                message_thread_id=topic_id,
                delete_scheduled_at=deletion_at,
            )
            await notif_repo.save_notification(notif)

        await MintRepository(self._session).update_status(
            db_record.id,
            MintStatus.NOTIFIED,
            {
                "notification_sent_at": utcnow(),
                "notification_message_id": primary_msg_id,
                "delete_scheduled_at": deletion_at,
            },
        )
        await self._session.commit()

        # Schedule deletion
        self._scheduler.schedule_deletion(
            mint_id=db_record.id,
            message_id=primary_msg_id,
            run_at=deletion_at,
            delete_fn=self._delete_scheduled,
        )

    async def _send_scheduled(self, mint_id: str) -> None:
        """Called by APScheduler when the scheduled notification time arrives."""
        from app.database.database import get_session_factory
        from sqlalchemy import select

        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(MintOpportunityDB).where(MintOpportunityDB.id == mint_id)
            )
            db_record = result.scalar_one_or_none()
            if db_record is None:
                log.error("[SCHEDULER] Mint not found for send job: %s", mint_id)
                return

            if db_record.status not in (MintStatus.SCHEDULED.value, MintStatus.VALIDATED.value):
                log.info(
                    "[SCHEDULER] Skipping send — status=%s mint_id=%s",
                    db_record.status,
                    mint_id,
                )
                return

            targets = await self._get_all_broadcast_targets(session)
            sent_messages: list[tuple[int, int, Optional[int]]] = []
            for chat_id, topic_id in targets:
                msg_id = await self._notifier.send_to_target(db_record, chat_id, topic_id)
                if msg_id:
                    sent_messages.append((chat_id, msg_id, topic_id))

            if not sent_messages:
                await MintRepository(session).update_status(mint_id, MintStatus.FAILED)
                await session.commit()
                return

            if self._health:
                self._health.notifications_sent += len(sent_messages)

            deletion_at = db_record.delete_scheduled_at
            notif_repo = NotificationRepository(session)
            primary_msg_id = sent_messages[0][1]

            for chat_id, msg_id, topic_id in sent_messages:
                notif = NotificationDB(
                    id=str(uuid.uuid4()),
                    mint_id=mint_id,
                    chat_id=chat_id,
                    message_id=msg_id,
                    message_thread_id=topic_id,
                    delete_scheduled_at=deletion_at,
                )
                await notif_repo.save_notification(notif)

            await MintRepository(session).update_status(
                mint_id,
                MintStatus.NOTIFIED,
                {
                    "notification_sent_at": utcnow(),
                    "notification_message_id": primary_msg_id,
                },
            )
            await session.commit()

        if deletion_at:
            self._scheduler.schedule_deletion(
                mint_id=mint_id,
                message_id=primary_msg_id,
                run_at=deletion_at,
                delete_fn=self._delete_scheduled,
            )

    async def _delete_scheduled(self, mint_id: str, message_id: int) -> None:
        """Called by APScheduler when the deletion time arrives."""
        from app.database.database import get_session_factory

        factory = get_session_factory()
        async with factory() as session:
            notif_repo = NotificationRepository(session)
            notifs = await notif_repo.get_all_by_mint_id(mint_id)
            deleted_any = False
            for notif in notifs:
                if notif.deleted_at is None:
                    ok = await self._notifier.delete_target_message(notif.chat_id, notif.message_id, mint_id)
                    if ok:
                        notif.deleted_at = utcnow()
                        deleted_any = True

            if not notifs:
                deleted_any = await self._notifier.delete_mint_alert(message_id, mint_id)

            if deleted_any and self._health:
                self._health.deletions += 1

            status = MintStatus.DELETED if deleted_any else MintStatus.FAILED
            extra = {"deleted_at": utcnow()} if deleted_any else {}
            await MintRepository(session).update_status(mint_id, status, extra)
            await session.commit()


# ---------------------------------------------------------------------------
# Raw data validation
# ---------------------------------------------------------------------------

def _validate_raw(raw: RawDrop, stage: RawDropStage) -> bool:
    """
    Validate that the raw drop and stage contain the minimum required fields.

    Logs [INVALID_DATA] and returns False if any required field is missing.
    """
    eid = stage.uuid if stage else "?"

    checks = [
        (raw.collection_slug, "collection_slug"),
        (raw.contract_address, "contract_address"),
        (raw.chain, "chain"),
        (raw.opensea_url, "mint_url"),
        (stage, "stage"),
    ]
    if stage:
        checks += [
            (stage.uuid, "stage.uuid"),
            (stage.stage_type, "stage.stage_type"),
            (stage.start_time, "stage.start_time"),
        ]

    for value, field_name in checks:
        if not value:
            log.warning(
                "[INVALID_DATA] external_id=%s chain=%s field=%s reason=missing_or_empty",
                eid,
                raw.chain if raw else "?",
                field_name,
            )
            return False

    # Validate start_time parses
    try:
        from app.providers.opensea import _parse_dt
        dt = _parse_dt(stage.start_time)
        if dt.tzinfo is None:
            raise ValueError("start_time must be timezone-aware")
    except Exception as exc:
        log.warning(
            "[INVALID_DATA] external_id=%s chain=%s field=start_time reason=%s",
            eid,
            raw.chain,
            exc,
        )
        return False

    return True
