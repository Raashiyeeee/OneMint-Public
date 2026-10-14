"""
Alert lifecycle — discovery, scheduling, pre-send re-check, send, delete,
reschedule and restart recovery.

Spec mapping
------------
Rule 1/2  eligibility + offer ratio      -> app.filters.mint_filter.MintFilter
Rule 3    discovery alert when qualified + reminder at start - 15 min
          -> AlertService.process / send_alert
          never at/after start for the reminder; re-check before reminder;
          only to TELEGRAM_GROUP_ID + PUBLIC_MINT_TOPIC_ID
Rule 4    delete at start + 10 min       -> AlertService.send_alert / delete_alert
Rule 5    dedupe, UTC, reschedule on start change, persistence + recovery
                                         -> AlertService.process / recover

The database row (MintOpportunityDB) is the source of truth.  APScheduler jobs
(persisted in the SQLAlchemy job store) only *trigger* work; every job re-reads
the row and decides from it, so a stale or duplicated job can never cause a
second reminder.

Lifecycle
---------
discovered ──(static rule fails)──────────────▶ REJECTED
     │──(found at/after start)────────────────▶ MISSED
     │──(qualified)──▶ discovery Telegram alert (immediate)
     ▼
 SCHEDULED ──(start - 15m, claim)─▶ SENDING ──(re-check fails)─▶ REJECTED
     ▲                                  │──(start moved)──▶ SCHEDULED (rescheduled)
     │                                  ▼
     └──(start moved)── NOTIFIED ──(start + 10m)──▶ DELETED
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Callable, Optional

from sqlalchemy import func, select, update

from app import rules
from app.config import Settings
from app.database.models import MintOpportunityDB, NotificationDB
from app.database.repository import NotificationRepository, _mint_to_db
from app.filters.mint_filter import FilterConfig, MintFilter
from app.models.mint import MintOpportunity, MintStatus
from app.providers.base import RawDrop, RawDropStage
from app.utils.time import ensure_utc, utcnow

if TYPE_CHECKING:
    from app.monitor.monitor import MonitorHealth
    from app.monitor.scheduler import MintScheduler

log = logging.getLogger(__name__)

# Tolerance for a job firing marginally early (clock jitter).
_EARLY_TOLERANCE = timedelta(seconds=5)

ACTIVE_UNSENT = (MintStatus.SCHEDULED.value, MintStatus.SENDING.value)
ACTIVE_SENT = (MintStatus.NOTIFIED.value, MintStatus.DELETE_PENDING.value)


def alert_time(start: datetime) -> datetime:
    return ensure_utc(start) - rules.ALERT_BEFORE_START


def deletion_time(start: datetime) -> datetime:
    return ensure_utc(start) + rules.DELETE_AFTER_START


class AlertService:
    """Owns the alert lifecycle. One instance per process (see set_alert_service)."""

    def __init__(
        self,
        settings: Settings,
        provider,
        notifier,
        scheduler: "MintScheduler",
        session_factory=None,
        health: Optional["MonitorHealth"] = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._settings = settings
        self._provider = provider
        self._notifier = notifier
        self._scheduler = scheduler
        self._session_factory = session_factory
        self._health = health
        self._clock = clock

    # ── helpers ───────────────────────────────────────────────────────────────

    def _factory(self):
        if self._session_factory is None:
            from app.database.database import get_session_factory
            return get_session_factory()
        return self._session_factory

    def _now(self) -> datetime:
        return ensure_utc(self._clock())

    def _filter(self) -> MintFilter:
        s = self._settings
        return MintFilter(
            FilterConfig(
                max_mint_price_usd=s.max_mint_price_usd,
                min_offer_multiplier=s.min_offer_multiplier,
                min_minted_percentage=s.min_minted_percentage,
                allow_sold_out=s.allow_sold_out,
            )
        )

    def _schedule_send(self, mint_id: str, at: datetime) -> None:
        self._scheduler.schedule_notification(mint_id=mint_id, run_at=at, send_fn=run_send_job)

    def _schedule_delete(self, mint_id: str, message_id: int, at: datetime) -> None:
        self._scheduler.schedule_deletion(
            mint_id=mint_id, message_id=message_id, run_at=at, delete_fn=run_delete_job
        )

    async def _fetch_normalized(
        self, raw: RawDrop, stage: RawDropStage, eth_price: Optional[Decimal]
    ) -> MintOpportunity:
        detailed = await self._provider.get_mint_details(raw.collection_slug)
        if detailed:
            if detailed.total_supply is not None:
                raw.total_supply = detailed.total_supply
            if detailed.max_supply is not None:
                raw.max_supply = detailed.max_supply
        offer = await self._provider.get_collection_offer_price(
            raw.collection_slug, chain=raw.chain, contract_address=raw.contract_address
        )
        return await self._provider.normalize_mint(
            raw, stage, float(eth_price) if eth_price is not None else None, offer_price_usd=offer
        )

    @staticmethod
    async def _find(session, mint: MintOpportunity) -> Optional[MintOpportunityDB]:
        """Dedupe key: (provider, chain, contract, stage uuid) == collection + mint phase."""
        stmt = select(MintOpportunityDB).where(
            MintOpportunityDB.provider == mint.provider,
            MintOpportunityDB.chain == mint.chain,
            func.lower(MintOpportunityDB.contract_address) == mint.contract_address.lower(),
            MintOpportunityDB.external_id == mint.external_id,
        )
        return (await session.execute(stmt)).scalars().first()

    @staticmethod
    def _apply_snapshot(rec: MintOpportunityDB, mint: MintOpportunity) -> None:
        rec.project_name = mint.project_name
        rec.mint_type = mint.mint_type
        rec.mint_price_eth = str(mint.mint_price_eth) if mint.mint_price_eth is not None else None
        rec.mint_price_usd = str(mint.mint_price_usd)
        rec.offer_price_usd = str(mint.offer_price_usd) if mint.offer_price_usd is not None else None
        rec.total_supply = mint.total_supply
        rec.minted_quantity = mint.minted_quantity
        rec.minted_percentage = str(mint.minted_percentage) if mint.minted_percentage is not None else None
        rec.mint_end_time = mint.mint_end_time
        rec.updated_at = utcnow()

    @staticmethod
    def _is_reminder(n: NotificationDB) -> bool:
        """True for the start-15m alert (legacy rows without kind count as reminders)."""
        return getattr(n, "kind", None) in (None, "", rules.ALERT_KIND_REMINDER)

    @staticmethod
    def _is_discovery(n: NotificationDB) -> bool:
        return getattr(n, "kind", None) == rules.ALERT_KIND_DISCOVERY

    async def _has_kind(self, session, mint_id: str, kind: str) -> bool:
        notifs = await NotificationRepository(session).get_all_by_mint_id(mint_id)
        if kind == rules.ALERT_KIND_DISCOVERY:
            return any(self._is_discovery(n) for n in notifs)
        return any(self._is_reminder(n) for n in notifs)

    async def _maybe_send_discovery(
        self,
        session,
        rec: MintOpportunityDB,
        mint: MintOpportunity,
        start: datetime,
        *,
        finalize: bool,
    ) -> Optional[int]:
        """Send the immediate qualify alert once. finalize=True when no -15m reminder will fire."""
        if await self._has_kind(session, rec.id, rules.ALERT_KIND_DISCOVERY):
            return None
        if await self._has_kind(session, rec.id, rules.ALERT_KIND_REMINDER):
            return None
        return await self._deliver(
            session, rec, mint, start, kind=rules.ALERT_KIND_DISCOVERY, finalize=finalize
        )

    # ── discovery / poll ──────────────────────────────────────────────────────

    async def process(
        self, raw: RawDrop, stage: RawDropStage, eth_price: Optional[Decimal]
    ) -> str:
        """Handle one (drop, stage) seen during a poll.

        When the mint fully qualifies, sends a discovery alert immediately and
        still schedules the start-15m reminder (if that window has not passed).

        Returns a short outcome string (useful for logs/tests).
        """
        if not _validate_raw(raw, stage):
            return "invalid"
        try:
            mint = await self._fetch_normalized(raw, stage, eth_price)
        except Exception as exc:
            log.warning("[INVALID_DATA] external_id=%s reason=%s", stage.uuid, exc)
            return "invalid"

        now = self._now()
        result = self._filter().evaluate(mint, now)
        static_fail = not result.passed and not result.is_dynamic_failure
        start = mint.mint_start_time
        notify_at = alert_time(start)

        async with self._factory()() as session:
            rec = await self._find(session, mint)

            if rec is None:
                if static_fail:
                    mint.status = MintStatus.REJECTED
                    rec = _mint_to_db(mint)
                    rec.rejection_reason = f"{result.rule}: {result.reason}"
                    session.add(rec)
                    await session.commit()
                    if self._health:
                        self._health.rejected += 1
                    log.info("[MINT_REJECTED] external_id=%s %s", mint.external_id, rec.rejection_reason)
                    return "rejected"
                if now >= start:
                    mint.status = MintStatus.MISSED
                    rec = _mint_to_db(mint)
                    rec.rejection_reason = "discovered at or after mint start"
                    session.add(rec)
                    await session.commit()
                    log.info("[MINT_MISSED] external_id=%s discovered after start", mint.external_id)
                    return "missed"

                can_schedule_reminder = now < notify_at
                mint.status = MintStatus.SCHEDULED
                mint.notification_scheduled_at = notify_at if can_schedule_reminder else None
                mint.delete_scheduled_at = deletion_time(start)
                rec = _mint_to_db(mint)
                rec.rejection_reason = None if result.passed else f"pending re-check: {result.reason}"
                session.add(rec)
                await session.commit()

                if result.passed:
                    await self._maybe_send_discovery(
                        session, rec, mint, start, finalize=not can_schedule_reminder
                    )
                    if self._health:
                        self._health.qualified += 1

                if can_schedule_reminder:
                    # Re-load status: discovery finalize may have marked NOTIFIED
                    await session.refresh(rec)
                    if rec.status == MintStatus.SCHEDULED.value:
                        self._schedule_send(rec.id, notify_at)
                    return "scheduled"

                # Inside the 15m window: discovery only (if qualified), no reminder job.
                await session.refresh(rec)
                if rec.status == MintStatus.NOTIFIED.value:
                    return "notified"
                rec.status = MintStatus.MISSED.value
                rec.rejection_reason = (
                    "discovered inside the 15-minute window without qualifying yet"
                )
                await session.commit()
                log.info("[MINT_MISSED] external_id=%s inside alert window, not qualified", mint.external_id)
                return "missed"

            # ── existing record: refresh, detect start-time change ──────────
            self._apply_snapshot(rec, mint)
            old_start = ensure_utc(rec.mint_start_time)
            start_changed = old_start != start
            if start_changed:
                log.info(
                    "[MINT_RESCHEDULE] mint_id=%s start %s -> %s",
                    rec.id, old_start.isoformat(), start.isoformat(),
                )
                rec.mint_start_time = start

            outcome = "unchanged"
            if rec.status in ACTIVE_SENT:
                if start_changed:
                    del_at = deletion_time(start)
                    rec.delete_scheduled_at = del_at
                    await _set_notification_delete_at(session, rec.id, del_at)
                    await session.commit()
                    self._schedule_delete(rec.id, rec.notification_message_id or 0, max(del_at, now))
                    return "deletion_rescheduled"
                await session.commit()
                return "already_sent"

            if rec.status in ACTIVE_UNSENT:
                if static_fail:
                    rec.status = MintStatus.REJECTED.value
                    rec.rejection_reason = f"{result.rule}: {result.reason}"
                    await session.commit()
                    self._scheduler.cancel_job(f"notify_{rec.id}")
                    return "rejected"
                rec.rejection_reason = None if result.passed else f"pending re-check: {result.reason}"
                if start_changed:
                    rec.delete_scheduled_at = deletion_time(start)
                    rec.notification_scheduled_at = notify_at
                    if now >= start:
                        rec.status = MintStatus.MISSED.value
                        rec.rejection_reason = "start time moved to the past"
                        await session.commit()
                        self._scheduler.cancel_job(f"notify_{rec.id}")
                        self._scheduler.cancel_job(f"delete_{rec.id}")
                        return "missed"
                    if now >= notify_at:
                        # Too late for reminder; still send discovery if newly qualified.
                        await session.commit()
                        self._scheduler.cancel_job(f"notify_{rec.id}")
                        if result.passed:
                            await self._maybe_send_discovery(
                                session, rec, mint, start, finalize=True
                            )
                            await session.refresh(rec)
                            if rec.status == MintStatus.NOTIFIED.value:
                                return "notified"
                        rec.status = MintStatus.MISSED.value
                        rec.rejection_reason = "start time moved inside the 15-minute alert window"
                        await session.commit()
                        self._scheduler.cancel_job(f"delete_{rec.id}")
                        return "missed"
                    await session.commit()
                    self._schedule_send(rec.id, notify_at)
                    self._scheduler.cancel_job(f"delete_{rec.id}")
                    if result.passed:
                        await self._maybe_send_discovery(
                            session, rec, mint, start, finalize=False
                        )
                    return "rescheduled"

                if result.passed and now < start:
                    await self._maybe_send_discovery(
                        session, rec, mint, start, finalize=now >= notify_at
                    )
                    await session.refresh(rec)
                    if rec.status == MintStatus.NOTIFIED.value:
                        self._scheduler.cancel_job(f"notify_{rec.id}")
                        return "notified"
                await session.commit()
                return outcome

            # REJECTED / MISSED / EXPIRED / FAILED — may become eligible again
            # (e.g. minted % rose, or start moved later), but only if a reminder
            # has never been sent for this phase.
            if (
                rec.status in (MintStatus.REJECTED.value, MintStatus.MISSED.value)
                and rec.notification_sent_at is None
                and not await self._has_kind(session, rec.id, rules.ALERT_KIND_REMINDER)
                and not static_fail
                and now < start
            ):
                can_schedule_reminder = now < notify_at
                rec.status = MintStatus.SCHEDULED.value
                rec.notification_scheduled_at = notify_at if can_schedule_reminder else None
                rec.delete_scheduled_at = deletion_time(start)
                rec.rejection_reason = None if result.passed else f"pending re-check: {result.reason}"
                await session.commit()
                if result.passed:
                    await self._maybe_send_discovery(
                        session, rec, mint, start, finalize=not can_schedule_reminder
                    )
                await session.refresh(rec)
                if can_schedule_reminder and rec.status == MintStatus.SCHEDULED.value:
                    self._schedule_send(rec.id, notify_at)
                    return "scheduled"
                if rec.status == MintStatus.NOTIFIED.value:
                    return "notified"
                return "scheduled"
            await session.commit()
            return outcome

    # ── send (rule 3) ─────────────────────────────────────────────────────────

    async def send_alert(self, mint_id: str) -> bool:
        """Fire the start-15m reminder for a phase. Returns True only if a message was sent."""
        now = self._now()
        async with self._factory()() as session:
            rec = await session.get(MintOpportunityDB, mint_id)
            if rec is None or rec.status != MintStatus.SCHEDULED.value:
                log.info("[ALERT_SKIP] mint_id=%s status=%s", mint_id, rec.status if rec else None)
                return False
            start = ensure_utc(rec.mint_start_time)
            notify_at = alert_time(start)

            if now < notify_at - _EARLY_TOLERANCE:
                self._schedule_send(mint_id, notify_at)
                return False
            if now >= start or now > notify_at + rules.SEND_GRACE:
                # Discovery may already have been sent; only miss the reminder.
                if await self._has_kind(session, mint_id, rules.ALERT_KIND_DISCOVERY):
                    rec.status = MintStatus.NOTIFIED.value
                    rec.rejection_reason = f"reminder window missed (now={now.isoformat()})"
                else:
                    rec.status = MintStatus.MISSED.value
                    rec.rejection_reason = f"alert window missed (now={now.isoformat()})"
                await session.commit()
                log.warning("[ALERT_MISSED] mint_id=%s", mint_id)
                return False

            # Atomic claim: only one job/process can move SCHEDULED -> SENDING.
            claimed = await session.execute(
                update(MintOpportunityDB)
                .where(MintOpportunityDB.id == mint_id, MintOpportunityDB.status == MintStatus.SCHEDULED.value)
                .values(status=MintStatus.SENDING.value)
            )
            await session.commit()
            if claimed.rowcount != 1:
                return False

            existing = await NotificationRepository(session).get_all_by_mint_id(mint_id)
            if any(self._is_reminder(n) for n in existing) or rec.notification_sent_at is not None:
                rec.status = MintStatus.NOTIFIED.value
                await session.commit()
                return False

            # ── re-check every condition with fresh data ────────────────────
            fresh = await self._recheck(rec)
            if isinstance(fresh, str):
                rec.status = MintStatus.REJECTED.value
                rec.rejection_reason = f"pre-send re-check failed: {fresh}"
                await session.commit()
                log.info("[ALERT_BLOCKED] mint_id=%s %s", mint_id, rec.rejection_reason)
                return False
            mint = fresh
            if mint.mint_start_time != start:
                new_start = mint.mint_start_time
                rec.mint_start_time = new_start
                rec.notification_scheduled_at = alert_time(new_start)
                rec.delete_scheduled_at = deletion_time(new_start)
                if self._now() < alert_time(new_start):
                    rec.status = MintStatus.SCHEDULED.value
                    await session.commit()
                    self._schedule_send(mint_id, alert_time(new_start))
                    return False
                rec.status = MintStatus.MISSED.value
                rec.rejection_reason = "start time changed at send time"
                await session.commit()
                return False
            if self._now() >= start:
                rec.status = MintStatus.MISSED.value
                rec.rejection_reason = "mint started during re-check"
                await session.commit()
                return False

            msg_id = await self._deliver(
                session, rec, mint, start, kind=rules.ALERT_KIND_REMINDER, finalize=True
            )
        return msg_id is not None

    async def _deliver(
        self,
        session,
        rec: MintOpportunityDB,
        mint: MintOpportunity,
        start: datetime,
        *,
        kind: str = rules.ALERT_KIND_REMINDER,
        finalize: bool = True,
    ) -> Optional[int]:
        """Send to the configured group/topic only, record it, schedule deletion at start+10.

        Discovery alerts keep status SCHEDULED (unless finalize=True) so the -15m
        reminder job can still fire. Reminder / finalized alerts mark NOTIFIED.
        """
        mint_id = rec.id
        self._apply_snapshot(rec, mint)
        rec.rejection_reason = None
        chat_id = self._settings.telegram_group_id
        topic_id = self._settings.public_mint_topic_id
        send_kwargs = {"kind": kind}
        try:
            msg_id = await self._notifier.send_to_target(rec, chat_id, topic_id, **send_kwargs)
        except TypeError:
            # Older fakes / callers without the kind kwarg.
            msg_id = await self._notifier.send_to_target(rec, chat_id, topic_id)
        if not msg_id:
            if kind == rules.ALERT_KIND_REMINDER or finalize:
                rec.status = MintStatus.FAILED.value
                rec.rejection_reason = "telegram send failed"
                await session.commit()
            return None
        del_at = deletion_time(start)
        session.add(
            NotificationDB(
                id=str(uuid.uuid4()),
                mint_id=mint_id,
                chat_id=chat_id,
                message_id=msg_id,
                message_thread_id=topic_id,
                kind=kind,
                delete_scheduled_at=del_at,
            )
        )
        rec.notification_message_id = msg_id
        rec.delete_scheduled_at = del_at
        if finalize or kind == rules.ALERT_KIND_REMINDER:
            rec.status = MintStatus.NOTIFIED.value
            rec.notification_sent_at = utcnow()
        # else: stay SCHEDULED so the reminder job can claim later
        await session.commit()
        if self._health:
            self._health.notifications_sent += 1
        self._schedule_delete(mint_id, msg_id, del_at)
        log.info(
            "[ALERT_SENT] mint_id=%s kind=%s message_id=%s delete_at=%s",
            mint_id, kind, msg_id, del_at.isoformat(),
        )
        return msg_id

    # ── manual /sendtoday ─────────────────────────────────────────────────────

    async def send_today(self) -> list[dict]:
        """Admin-triggered early reminder alerts for today's (UTC) upcoming mints.

        Each candidate is re-checked on fresh data; passing ones get the reminder
        sent now to the configured group/topic only, recorded as NOTIFIED (so the
        automatic start-15m job will skip them) and get deletion at start+10.
        Discovery alerts alone do not block this — /sendtoday still sends the
        reminder early. Returns [{mint_id, name, start, sent: bool, reason}].
        """
        now = self._now()
        day_end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        results: list[dict] = []
        async with self._factory()() as session:
            rows = (
                await session.execute(
                    select(MintOpportunityDB).where(
                        MintOpportunityDB.status.notin_(
                            [MintStatus.DELETED.value, MintStatus.EXPIRED.value]
                        )
                    )
                )
            ).scalars().all()
            # Filter in Python on UTC-aware values (DB drivers differ on tz handling).
            rows = sorted(
                (r for r in rows if now < ensure_utc(r.mint_start_time) < day_end),
                key=lambda r: ensure_utc(r.mint_start_time),
            )
            ids = [r.id for r in rows]

        for mint_id in ids:
            async with self._factory()() as session:
                rec = await session.get(MintOpportunityDB, mint_id)
                start = ensure_utc(rec.mint_start_time)
                item = {"mint_id": mint_id, "name": rec.project_name, "start": start, "sent": False, "reason": ""}
                results.append(item)
                if self._now() >= start:
                    item["reason"] = "already started"
                    continue
                notifs = await NotificationRepository(session).get_all_by_mint_id(mint_id)
                reminder_done = (
                    rec.notification_sent_at is not None
                    or any(self._is_reminder(n) for n in notifs)
                    or rec.status in ACTIVE_SENT + (MintStatus.DELETED.value,)
                )
                if reminder_done:
                    item["reason"] = "already alerted"
                    continue
                prev_status = rec.status
                claimed = await session.execute(
                    update(MintOpportunityDB)
                    .where(
                        MintOpportunityDB.id == mint_id,
                        MintOpportunityDB.status == prev_status,
                        MintOpportunityDB.notification_sent_at.is_(None),
                    )
                    .values(status=MintStatus.SENDING.value)
                )
                await session.commit()
                if claimed.rowcount != 1:
                    item["reason"] = "being sent by another job"
                    continue
                fresh = await self._recheck(rec)
                if isinstance(fresh, str):
                    rec.status = prev_status  # leave the automatic path untouched
                    await session.commit()
                    item["reason"] = fresh
                    continue
                if fresh.mint_start_time != start:
                    rec.status = prev_status
                    await session.commit()
                    item["reason"] = "start time changed on fresh data; will be rescheduled by next poll"
                    continue
                if self._now() >= start:
                    rec.status = prev_status
                    await session.commit()
                    item["reason"] = "started during re-check"
                    continue
                msg_id = await self._deliver(
                    session, rec, fresh, start, kind=rules.ALERT_KIND_REMINDER, finalize=True
                )
                if msg_id is None:
                    item["reason"] = "telegram send failed"
                    continue
                self._scheduler.cancel_job(f"notify_{mint_id}")
                item["sent"] = True
                item["reason"] = f"sent (message {msg_id})"
        return results

    async def _recheck(self, rec: MintOpportunityDB):
        """Return a fresh MintOpportunity that passes every rule, or a reason string."""
        try:
            details = await self._provider.get_mint_details(rec.collection_slug)
        except Exception as exc:  # pragma: no cover - provider already swallows
            return f"could not refetch drop: {exc}"
        if details is None:
            return "could not refetch drop to confirm"
        stage = None
        for s in [details.next_stage, details.active_stage, *(details.stages or [])]:
            if s is not None and s.uuid == rec.external_id:
                stage = s
                break
        if stage is None:
            return "mint phase no longer listed by provider"
        if not _validate_raw(details, stage):
            return "refetched data invalid"
        eth = await self._provider.get_reliable_eth_price_usd()
        offer = await self._provider.get_collection_offer_price(
            details.collection_slug, chain=details.chain, contract_address=details.contract_address
        )
        try:
            mint = await self._provider.normalize_mint(
                details, stage, float(eth) if eth is not None else None, offer_price_usd=offer
            )
        except Exception as exc:
            return f"normalise failed: {exc}"
        if mint.mint_start_time != ensure_utc(rec.mint_start_time):
            return mint  # caller reschedules
        result = self._filter().evaluate(mint, self._now())
        if not result.passed:
            return f"{result.rule}: {result.reason}"
        return mint

    # ── delete (rule 4) ───────────────────────────────────────────────────────

    async def delete_alert(self, mint_id: str) -> bool:
        now = self._now()
        async with self._factory()() as session:
            rec = await session.get(MintOpportunityDB, mint_id)
            if rec is None:
                return False
            notifs = await NotificationRepository(session).get_all_by_mint_id(mint_id)
            # Allow deletion after discovery-only (still SCHEDULED) or after NOTIFIED.
            if rec.status not in ACTIVE_SENT and not (
                rec.status in ACTIVE_UNSENT and notifs
            ):
                return False
            del_at = deletion_time(rec.mint_start_time)
            if now < del_at - _EARLY_TOLERANCE:
                # Start was postponed after this job was queued.
                self._schedule_delete(mint_id, rec.notification_message_id or 0, del_at)
                return False

            all_ok = True
            for n in notifs:
                if n.deleted_at is None:
                    ok = await self._notifier.delete_target_message(n.chat_id, n.message_id, mint_id)
                    if ok:
                        n.deleted_at = utcnow()
                    all_ok = all_ok and ok
            if not notifs and rec.notification_message_id:
                all_ok = await self._notifier.delete_target_message(
                    self._settings.telegram_group_id, rec.notification_message_id, mint_id
                )
            if all_ok:
                rec.status = MintStatus.DELETED.value
                rec.deleted_at = utcnow()
            else:
                rec.status = MintStatus.FAILED.value
                rec.rejection_reason = "telegram delete failed"
            await session.commit()
        if all_ok and self._health:
            self._health.deletions += 1
        return all_ok

    # ── restart recovery (rule 5) ─────────────────────────────────────────────

    async def recover(self) -> dict:
        """Re-arm every pending job from the DB and run overdue deletions now."""
        now = self._now()
        stats = {"alerts_rescheduled": 0, "alerts_missed": 0, "deletions_rescheduled": 0, "deleted_overdue": 0}
        async with self._factory()() as session:
            rows = (
                await session.execute(
                    select(MintOpportunityDB).where(
                        MintOpportunityDB.status.in_(ACTIVE_UNSENT + ACTIVE_SENT)
                    )
                )
            ).scalars().all()
            overdue: list[str] = []
            for rec in rows:
                start = ensure_utc(rec.mint_start_time)
                if rec.status in ACTIVE_UNSENT:
                    notifs = await NotificationRepository(session).get_all_by_mint_id(rec.id)
                    if any(self._is_reminder(n) for n in notifs) or rec.notification_sent_at is not None:
                        # Crashed after reminder send but before marking NOTIFIED.
                        rec.status = MintStatus.NOTIFIED.value
                        rec.notification_message_id = notifs[0].message_id if notifs else rec.notification_message_id
                    else:
                        # No reminder yet (discovery-only or nothing sent): re-arm -15m job.
                        notify_at = alert_time(start)
                        rec.status = MintStatus.SCHEDULED.value
                        if now < notify_at:
                            self._schedule_send(rec.id, notify_at)
                            stats["alerts_rescheduled"] += 1
                            if notifs:
                                # Discovery already sent — still need deletion at start+10.
                                del_at = deletion_time(start)
                                rec.delete_scheduled_at = del_at
                                self._schedule_delete(rec.id, rec.notification_message_id or 0, del_at)
                                stats["deletions_rescheduled"] += 1
                            continue
                        if now <= notify_at + rules.SEND_GRACE and now < start:
                            self._schedule_send(rec.id, now)
                            stats["alerts_rescheduled"] += 1
                            continue
                        if notifs:
                            # Discovery was sent; reminder window is gone.
                            rec.status = MintStatus.NOTIFIED.value
                            rec.notification_message_id = notifs[0].message_id
                        else:
                            rec.status = MintStatus.MISSED.value
                            rec.rejection_reason = "alert window passed while bot was offline"
                            self._scheduler.cancel_job(f"notify_{rec.id}")
                            stats["alerts_missed"] += 1
                            continue
                # sent (or discovery+missed reminder): delete at start + 10
                del_at = deletion_time(start)
                rec.delete_scheduled_at = del_at
                if now >= del_at:
                    overdue.append(rec.id)
                else:
                    self._schedule_delete(rec.id, rec.notification_message_id or 0, del_at)
                    stats["deletions_rescheduled"] += 1
            await session.commit()
        for mint_id in overdue:
            self._scheduler.cancel_job(f"delete_{mint_id}")
            if await self.delete_alert(mint_id):
                stats["deleted_overdue"] += 1
        log.info("[RECOVERY] %s", stats)
        return stats


async def _set_notification_delete_at(session, mint_id: str, del_at: datetime) -> None:
    await session.execute(
        update(NotificationDB)
        .where(NotificationDB.mint_id == mint_id, NotificationDB.deleted_at.is_(None))
        .values(delete_scheduled_at=del_at)
    )


# ---------------------------------------------------------------------------
# Module-level APScheduler job entry points (picklable by reference).
# ---------------------------------------------------------------------------

_service: Optional[AlertService] = None


def set_alert_service(service: Optional[AlertService]) -> None:
    global _service
    _service = service


def get_alert_service() -> Optional[AlertService]:
    return _service


async def run_send_job(mint_id: str) -> None:
    if _service is None:
        log.error("[SCHEDULER] send job for %s fired before AlertService was ready", mint_id)
        return
    await _service.send_alert(mint_id)


async def run_delete_job(mint_id: str, message_id: int = 0) -> None:
    if _service is None:
        log.error("[SCHEDULER] delete job for %s fired before AlertService was ready", mint_id)
        return
    await _service.delete_alert(mint_id)


# Backwards-compatible names: jobs already persisted by older versions
# reference these symbols in the APScheduler job store.
_run_send_job = run_send_job
_run_delete_job = run_delete_job


# ---------------------------------------------------------------------------
# Raw data validation
# ---------------------------------------------------------------------------

def _validate_raw(raw: RawDrop, stage: RawDropStage) -> bool:
    """Minimum required fields, and a parseable timezone-aware start time."""
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
            log.warning("[INVALID_DATA] external_id=%s field=%s reason=missing_or_empty", eid, field_name)
            return False
    try:
        dt = datetime.fromisoformat(stage.start_time.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("start_time must carry a timezone (unconfirmed otherwise)")
    except Exception as exc:
        log.warning("[INVALID_DATA] external_id=%s field=start_time reason=%s", eid, exc)
        return False
    return True
