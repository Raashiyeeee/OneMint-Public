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
import tempfile
import fcntl

from app.bot.bot import build_application
from app.config import get_settings
from app.database.database import create_tables, dispose_engine, init_engine
from app.monitor.monitor import MintMonitor
from app.monitor.scheduler import MintScheduler
from app.providers.opensea import OpenSeaProvider
from app.services.notification_service import TelegramNotificationService
from app.utils.logging import setup_logging

log = logging.getLogger(__name__)


# Module-level PID lock file handle — kept open for the process lifetime
_pid_lock_fh = None


def _acquire_pid_lock() -> None:
    """Acquire an exclusive flock-based lock to prevent duplicate instances.

    The lock file is placed in the system temp dir so it survives across
    working-directory changes but is cleaned up on reboot.
    """
    global _pid_lock_fh
    lock_path = os.path.join(tempfile.gettempdir(), "public_mint_bot.lock")
    _pid_lock_fh = open(lock_path, "w")  # noqa: WPS515
    try:
        fcntl.flock(_pid_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _pid_lock_fh.write(str(os.getpid()))
        _pid_lock_fh.flush()
    except BlockingIOError:
        _pid_lock_fh.close()
        sys.exit(
            "ERROR: Another instance of Public Mint Bot is already running "
            f"(lock file: {lock_path}). "
            "Stop the other instance before starting a new one."
        )


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

    # Delete any stale webhook — a registered webhook causes Conflict errors
    # when you switch back to polling without explicitly removing it first.
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        log.info("[BOT] Stale webhook cleared (if any)")
    except Exception as exc:  # noqa: BLE001
        log.warning("[BOT] Could not delete webhook: %s", exc)

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

    # Load runtime admin IDs from database and merge with configured admin IDs
    from app.bot.handlers import _load_runtime_admin_ids
    from telegram import Update

    runtime_admins = await _load_runtime_admin_ids()
    merged_admin_ids = list(dict.fromkeys(settings.admin_ids + runtime_admins))

    # Inject shared state into bot context
    tg_app.bot_data["settings"] = settings
    tg_app.bot_data["monitor"] = monitor
    tg_app.bot_data["admin_ids"] = merged_admin_ids

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

        webhook_url = (settings.webhook_url or "").rstrip("/")
        web_runner = None
        port = int(os.environ.get("PORT", str(settings.webhook_port or 10000)))

        import aiohttp.web
        health_app = aiohttp.web.Application()
        health_app.router.add_get("/", lambda r: aiohttp.web.Response(text="Bot is running!"))
        health_app.router.add_get("/health", lambda r: aiohttp.web.Response(text="OK"))

        if webhook_url:
            # ── Webhook mode (Render / cloud hosts with public HTTPS) ──────────
            webhook_path = f"/webhook/{settings.telegram_bot_token}"
            full_webhook_url = f"{webhook_url}{webhook_path}"

            async def handle_telegram_webhook(request: aiohttp.web.Request) -> aiohttp.web.Response:
                try:
                    data = await request.json()
                    update = Update.de_json(data, bot)
                    if update:
                        await tg_app.update_queue.put(update)
                    return aiohttp.web.Response(status=200)
                except Exception as exc:
                    log.error("[WEBHOOK] Error handling update: %s", exc)
                    return aiohttp.web.Response(status=500)

            health_app.router.add_post(webhook_path, handle_telegram_webhook)

            # Register webhook with Telegram
            await bot.set_webhook(
                url=full_webhook_url,
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=False,
            )
            log.info("[BOT] Webhook registered with Telegram → %s", full_webhook_url)

        else:
            # ── Polling mode (local dev, Docker, systemd) ──────────────────────
            try:
                await bot.delete_webhook(drop_pending_updates=False)
            except Exception as exc:
                log.warning("[BOT] Could not delete webhook: %s", exc)

            await tg_app.updater.start_polling(
                drop_pending_updates=False,
                allowed_updates=Update.ALL_TYPES,
            )
            log.info("[BOT] Polling mode active")

        # Start aiohttp server (serves /health for Render and /webhook for Telegram)
        if port > 0:
            web_runner = aiohttp.web.AppRunner(health_app)
            await web_runner.setup()
            site = aiohttp.web.TCPSite(web_runner, "0.0.0.0", port)
            await site.start()
            log.info("[WEB] HTTP server listening on port %d (/health ready)", port)

        # Start monitor in background
        monitor_task = asyncio.create_task(monitor.start(), name="monitor")

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

        if tg_app.updater and tg_app.updater.running:
            await tg_app.updater.stop()
        await tg_app.stop()
        log.info("[BOT] Telegram stopped")

    await dispose_engine()
    log.info("[DB] Engine disposed")
    log.info("PUBLIC MINT LINK BOT — shutdown complete")


def main() -> None:
    """Entry point for direct execution."""
    _acquire_pid_lock()  # Fail fast if another instance is already running
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
