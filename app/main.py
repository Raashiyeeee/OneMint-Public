"""
Application entry point for the Public Mint Link Bot.

Startup sequence
----------------
1. Load settings from environment
2. Configure logging
3. Initialise database (create tables)
4. Start APScheduler (persistent job store)
5. Start Telegram bot (polling, non-blocking)
6. Start monitoring loop (background task)
7. Wait for SIGTERM/SIGINT
8. Graceful shutdown

All components share the same asyncio event loop.
The Telegram bot remains responsive while the monitor runs.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from app.bot.bot import build_application
from app.config import get_settings
from app.database.database import create_tables, dispose_engine, init_engine
from app.monitor.monitor import MintMonitor
from app.monitor.scheduler import MintScheduler
from app.providers.opensea import OpenSeaProvider
from app.services.notification_service import TelegramNotificationService
from app.utils.logging import setup_logging

log = logging.getLogger(__name__)


async def run() -> None:
    """Main async entry point."""
    settings = get_settings()
    setup_logging(settings.log_level)

    log.info("=" * 60)
    log.info("PUBLIC MINT LINK BOT — starting up")
    log.info("Test mode: %s", settings.test_mode)
    log.info("Chains: %s", settings.enabled_chains)
    log.info("Poll interval: %ds", settings.poll_interval_seconds)
    log.info("=" * 60)

    # ── Database ──────────────────────────────────────────────────────────────
    init_engine(settings.database_url)
    await create_tables()
    log.info("[DB] Tables created/verified")

    # ── Provider ──────────────────────────────────────────────────────────────
    provider = OpenSeaProvider(
        api_key=settings.opensea_api_key,
        base_url=settings.opensea_api_endpoint,
    )

    # ── Scheduler ─────────────────────────────────────────────────────────────
    # APScheduler needs a sync SQLite URL for its job store
    scheduler_url = settings.database_url.replace("sqlite+aiosqlite", "sqlite")
    scheduler = MintScheduler(db_url=scheduler_url)

    # ── Telegram bot ──────────────────────────────────────────────────────────
    tg_app = build_application(settings.telegram_bot_token)
    bot = tg_app.bot

    notifier = TelegramNotificationService(
        bot=bot,
        chat_id=settings.telegram_group_id,
        topic_id=settings.public_mint_topic_id,
    )

    # ── Monitor ───────────────────────────────────────────────────────────────
    monitor = MintMonitor(
        settings=settings,
        provider=provider,
        scheduler=scheduler,
        notifier=notifier,
    )

    # Inject shared state into bot context
    tg_app.bot_data["settings"] = settings
    tg_app.bot_data["monitor"] = monitor
    tg_app.bot_data["admin_ids"] = settings.admin_ids

    # ── Graceful shutdown handler ─────────────────────────────────────────────
    shutdown_event = asyncio.Event()

    def _handle_signal() -> None:
        log.info("Shutdown signal received")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle_signal)

    # ── Start components ───────────────────────────────────────────────────────
    async with tg_app:
        await tg_app.start()
        await tg_app.updater.start_polling(drop_pending_updates=True)
        log.info("[BOT] Telegram polling started")

        # Start monitor in background
        monitor_task = asyncio.create_task(monitor.start(), name="monitor")

        # Optional health check server for cloud hosting (Render, Koyeb, Railway)
        port = int(os.environ.get("PORT", "0"))
        web_runner = None
        if port > 0:
            import aiohttp.web
            web_app = aiohttp.web.Application()
            web_app.router.add_get("/", lambda r: aiohttp.web.Response(text="Bot is running!"))
            web_app.router.add_get("/health", lambda r: aiohttp.web.Response(text="OK"))
            web_runner = aiohttp.web.AppRunner(web_app)
            await web_runner.setup()
            site = aiohttp.web.TCPSite(web_runner, "0.0.0.0", port)
            await site.start()
            log.info("[WEB] Health check server listening on port %d", port)

        # Wait until shutdown is requested
        await shutdown_event.wait()

        # ── Graceful shutdown ─────────────────────────────────────────────────
        log.info("Initiating graceful shutdown...")
        if web_runner:
            await web_runner.cleanup()

        await monitor.stop()
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass

        await tg_app.updater.stop()
        await tg_app.stop()
        log.info("[BOT] Telegram stopped")

    await dispose_engine()
    log.info("[DB] Engine disposed")
    log.info("PUBLIC MINT LINK BOT — shutdown complete")


def main() -> None:
    """Entry point for direct execution."""
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
