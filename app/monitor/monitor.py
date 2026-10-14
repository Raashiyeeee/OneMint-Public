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
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from app.chains.registry import ChainConfig, get_chain
from app.config import Settings
from app.database.database import get_session_factory
from app.database.models import MintOpportunityDB, NotificationDB
from app.database.repository import SystemStateRepository
from app.monitor.processor import AlertService, set_alert_service
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
        self.alerts = AlertService(
            settings=settings,
            provider=provider,
            notifier=notifier,
            scheduler=scheduler,
            health=self.health,
        )
        set_alert_service(self.alerts)

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
        # Only a live rate is used for USD thresholds; None => prices unreliable.
        eth_price = await self._provider.get_reliable_eth_price_usd()
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
        eth_price: Optional[Decimal],
    ) -> None:
        """Process a single drop+stage. Discovery never sends an alert."""
        await self.alerts.process(raw_drop, stage, eth_price)

    async def _recover_on_startup(self) -> None:
        """Re-arm pending alerts/deletions and delete overdue messages."""
        log.info("[MONITOR_CYCLE] Running startup recovery scan...")
        await self.alerts.recover()
