"""
Telegram bot handlers.

Commands:
  /start        — Greet the user (everyone)
  /help         — List all available commands (everyone)
  /status       — Show bot/monitor status (admin only)
  /monitor      — Toggle monitoring on/off (admin only)
  /test         — Send a test alert to the topic (admin only)
  /stats        — Show today's mint stats from DB (admin only)
  /chains       — Show all monitored chains and their status (admin only)
  /filters      — Show current filter settings (admin only)
  /setfilter    — Change a filter value at runtime (admin only)
  /recent       — Show last 5 qualified mints (admin only)
  /ping         — Health check — bot responds with pong (admin only)
  /jobs         — List all pending APScheduler jobs (admin only)
  /addadmin     — Add a new admin by user ID (super-admin only)
  /removeadmin  — Remove an admin by user ID (super-admin only)
  /admins       — List all current admins (admin only)

Admin restriction:
  If ADMIN_USER_IDS is set, all commands except /start and /help
  are restricted to those user IDs.

Super-admin:
  The FIRST user ID in ADMIN_USER_IDS is the super-admin and is the
  only one who can add/remove other admins.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from telegram import Update
from telegram.ext import ContextTypes

if TYPE_CHECKING:
    from app.monitor.monitor import MintMonitor

log = logging.getLogger(__name__)

FILTER_FIELDS = {
    "max_mint_price_usd": ("MAX_MINT_PRICE_USD", "Maximum mint price in USD (0-20)"),
    "min_offer_multiplier": ("MIN_OFFER_MULTIPLIER", "Offer/price ratio for paid mints (e.g. 1.5)"),
    "free_mint_min_offer_usd": ("FREE_MINT_MIN_OFFER_USD", "Min offer for free mints (e.g. 5)"),
    "min_minted_percentage": ("MIN_MINTED_PERCENTAGE", "Min minted % to qualify (0-100)"),
    "allow_sold_out": ("ALLOW_SOLD_OUT", "Allow 100% sold-out mints (true/false)"),
    "notification_before_minutes": ("NOTIFICATION_BEFORE_MINUTES", "Minutes before mint start to alert (e.g. 10)"),
    "delete_after_minutes": ("DELETE_AFTER_MINUTES", "Minutes after mint start to delete notification (e.g. 15)"),
    "poll_interval_seconds": ("POLL_INTERVAL_SECONDS", "Fetching/polling interval in seconds (30-86400)"),
}

DB_ADMIN_KEY = "admin_ids_runtime"


def _is_admin(user_id: int, admin_ids: list[int]) -> bool:
    return not admin_ids or user_id in admin_ids


def _admin_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Return True if the user is authorised."""
    admin_ids: list[int] = context.bot_data.get("admin_ids", [])
    if not update.effective_user:
        return False
    if admin_ids and update.effective_user.id not in admin_ids:
        return False
    return True


def _should_reply_unauthorized(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Return True if the bot should send an ⛔ reply to non-admins.
    When RESTRICT_TO_ADMINS=true, stay silent for admin-only commands."""
    settings = context.bot_data.get("settings")
    restrict = getattr(settings, "restrict_to_admins", False) if settings else False
    return not restrict  # reply only when NOT in lockdown mode


async def _reject_unauthorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send an unauthorized notice if configured to do so."""
    if _should_reply_unauthorized(context) and update.effective_message:
        user_id = update.effective_user.id if update.effective_user else "unknown"
        await update.effective_message.reply_text(
            f"⛔ <b>Unauthorised</b>\nYour Telegram User ID is: <code>{user_id}</code>.\n"
            "This command is restricted to administrators.",
            parse_mode="HTML",
        )


def _is_super_admin(user_id: int, admin_ids: list[int]) -> bool:
    """Super-admin = first user in admin_ids list. Only they can add/remove admins."""
    return bool(admin_ids) and admin_ids[0] == user_id


async def _persist_admin_ids(context: ContextTypes.DEFAULT_TYPE, admin_ids: list[int]) -> None:
    """Persist the runtime admin list to the database so it survives restarts."""
    try:
        from app.database.database import get_session_factory
        from app.database.repository import SystemStateRepository
        factory = get_session_factory()
        async with factory() as session:
            repo = SystemStateRepository(session)
            await repo.set(DB_ADMIN_KEY, ",".join(str(i) for i in admin_ids))
            await session.commit()
    except Exception as exc:
        log.warning("[BOT] Failed to persist admin_ids: %s", exc)


async def _load_runtime_admin_ids() -> list[int]:
    """Load runtime-added admin IDs from database on startup."""
    try:
        from app.database.database import get_session_factory
        from app.database.repository import SystemStateRepository
        factory = get_session_factory()
        async with factory() as session:
            repo = SystemStateRepository(session)
            value = await repo.get(DB_ADMIN_KEY)
            if value:
                return [int(i) for i in value.split(",") if i.strip().isdigit()]
    except Exception as exc:
        log.warning("[BOT] Failed to load runtime admin_ids: %s", exc)
    return []


# ── /start ─────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start. Always greet the user and display status / user ID."""
    if not update.effective_message:
        return
    user_id = update.effective_user.id if update.effective_user else "unknown"
    is_admin = _admin_check(update, context)

    if is_admin:
        role_badge = f"✅ <b>Authorised Admin</b> (User ID: <code>{user_id}</code>)"
        help_tip = "Use /help to see all available commands, or /status to check health."
    else:
        role_badge = (
            f"ℹ️ <b>Your Telegram User ID:</b> <code>{user_id}</code>\n"
            f"⚠️ <i>Administrative commands are restricted to authorised admins. "
            f"To grant admin access, add this ID (<code>{user_id}</code>) to <code>ADMIN_USER_IDS</code>.</i>"
        )
        help_tip = "Use /ping to check bot connectivity."

    text = (
        "👋 <b>Public Mint Link Bot</b> is online!\n\n"
        f"{role_badge}\n\n"
        "I monitor OpenSea for qualifying public NFT mints across 15 chains "
        "and post alerts to configured broadcast destinations — 10 minutes before each mint starts.\n\n"
        f"{help_tip}"
    )
    await update.effective_message.reply_text(text, parse_mode="HTML")


# ── /help ──────────────────────────────────────────────────────────────────────

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help. Shows commands available to the user."""
    if not update.effective_message:
        return
    is_admin = _admin_check(update, context)

    lines = [
        "📖 <b>Public Mint Link Bot — Commands</b>",
        "",
        "<b>General</b>",
        "/start — Welcome message and user info",
        "/help  — This help message",
        "/ping  — Check if bot is alive",
    ]

    if is_admin:
        lines.extend([
            "",
            "<b>Monitoring (Admin)</b>",
            "/status  — Bot health and live metrics",
            "/monitor on|off — Enable or pause monitoring",
            "/jobs    — Show all pending scheduled jobs",
            "",
            "<b>Data (Admin)</b>",
            "/stats   — Today's mint discovery stats",
            "/recent  — Last 5 qualified mints",
            "/chains  — All chains and their API status",
            "",
            "<b>Configuration (Admin)</b>",
            "/filters — Show current filter settings",
            "/setfilter &lt;key&gt; &lt;value&gt; — Change a filter value",
            "/setinterval &lt;time&gt; — Change fetch interval (e.g. 30m, 1h)",
            "",
            "<b>Testing & Destinations (Admin)</b>",
            "/targets   — List all broadcast groups/channels",
            "/addtarget &lt;chat_id&gt; [topic_id] [label] — Add destination",
            "/removetarget &lt;chat_id&gt; [topic_id] — Remove destination",
            "/test — Send a mock alert to all targets",
            "",
            "<b>Admins (Super-Admin)</b>",
            "/admins       — List all current admins",
            "/addadmin     — Add a new admin by user ID",
            "/removeadmin  — Remove an admin by user ID",
        ])
    else:
        user_id = update.effective_user.id if update.effective_user else "unknown"
        lines.extend([
            "",
            f"🔒 <i>Note: Administrative commands are restricted. Your User ID is <code>{user_id}</code>.</i>",
        ])

    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


# ── /ping ──────────────────────────────────────────────────────────────────────

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /ping — quick liveness check for everyone."""
    if not update.effective_message:
        return
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    user_id = update.effective_user.id if update.effective_user else "unknown"
    is_admin = _admin_check(update, context)
    badge = " (Admin)" if is_admin else f" (User ID: <code>{user_id}</code>)"
    await update.effective_message.reply_text(
        f"🏓 Pong! Bot is alive{badge}.\n<code>{now}</code>",
        parse_mode="HTML",
    )


# ── /status ────────────────────────────────────────────────────────────────────

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status — admin only."""
    if not _admin_check(update, context):
        await _reject_unauthorized(update, context)
        return

    monitor: MintMonitor | None = context.bot_data.get("monitor")
    health = monitor.health.to_dict() if monitor else {}
    settings = context.bot_data.get("settings")
    enabled = "✅ Enabled" if (monitor and monitor._enabled) else "❌ Paused"

    eth_price = monitor.last_eth_price if monitor else None
    if eth_price is None and monitor and monitor._provider:
        try:
            eth_price = await asyncio.wait_for(monitor._provider.get_eth_price_usd(), timeout=3.0)
        except Exception:
            pass
    eth_str = f"${eth_price:,.2f}" if eth_price else "Unavailable"

    interval_s = settings.poll_interval_seconds if settings else 60
    interval_m = interval_s / 60
    interval_str = f"{interval_m:.1f}m ({interval_s}s)" if interval_m >= 1 else f"{interval_s}s"

    last_poll_raw = health.get("last_api_request")
    last_poll_str = "never"
    next_poll_str = "N/A"
    if last_poll_raw and last_poll_raw != "never":
        try:
            lp_dt = datetime.fromisoformat(last_poll_raw)
            now = datetime.now(timezone.utc)
            ago_s = max(0, int((now - lp_dt).total_seconds()))
            if ago_s < 60:
                last_poll_str = f"{lp_dt.strftime('%H:%M:%S UTC')} ({ago_s}s ago)"
            else:
                last_poll_str = f"{lp_dt.strftime('%H:%M:%S UTC')} ({ago_s // 60}m ago)"

            remaining = interval_s - ago_s
            if remaining > 0:
                next_poll_str = f"in ~{remaining}s" if remaining < 60 else f"in ~{remaining // 60}m"
            else:
                next_poll_str = "in progress / due"
        except Exception:
            last_poll_str = str(last_poll_raw)

    lines = [
        "📊 <b>Bot Status</b>",
        "",
        "🤖 Status: <b>Running</b>",
        f"📡 Monitoring: <b>{enabled}</b>",
        f"⏱ Poll Interval: <b>{interval_str}</b>",
        f"⏳ Next Poll: <b>{next_poll_str}</b>",
        f"💰 ETH/USD: <b>{eth_str}</b>",
        "",
        "📈 <b>Session Metrics</b>",
        f"Last Poll:      <code>{last_poll_str}</code>",
        f"API Errors:     {health.get('api_errors', 0)}",
        f"Discovered:     {health.get('discovered', 0)}",
        f"Rejected:       {health.get('rejected', 0)}",
        f"Qualified:      {health.get('qualified', 0)}",
        f"Alerts Sent:    {health.get('notifications_sent', 0)}",
        f"Auto-Deleted:   {health.get('deletions', 0)}",
        f"Failed Ops:     {health.get('failed_ops', 0)}",
    ]
    await (update.effective_message or update.message).reply_text("\n".join(lines), parse_mode="HTML")


# ── /monitor ───────────────────────────────────────────────────────────────────

async def cmd_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /monitor on|off — admin only."""
    if not _admin_check(update, context):
        await _reject_unauthorized(update, context)
        return

    args = context.args
    if not args or args[0].lower() not in ("on", "off"):
        await (update.effective_message or update.message).reply_text("Usage: /monitor on | /monitor off")
        return

    monitor: MintMonitor | None = context.bot_data.get("monitor")
    if monitor is None:
        await (update.effective_message or update.message).reply_text("❌ Monitor not initialised.")
        return

    action = args[0].lower()
    monitor.set_enabled(action == "on")
    if action == "on":
        await (update.effective_message or update.message).reply_text("✅ Monitoring <b>enabled</b> — polling resumed.", parse_mode="HTML")
    else:
        await (update.effective_message or update.message).reply_text("⏸ Monitoring <b>paused</b> — no alerts will be sent until re-enabled.", parse_mode="HTML")


# ── /chains ────────────────────────────────────────────────────────────────────

async def cmd_chains(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /chains — show all 15 chains and their support status (admin only)."""
    if not _admin_check(update, context):
        await _reject_unauthorized(update, context)
        return

    from app.chains.registry import CHAINS
    lines = ["⛓ <b>Monitored Chains</b>", ""]
    for slug, chain in CHAINS.items():
        icon = "✅" if chain.provider_supported else "⚠️"
        api_id = chain.api_identifier if chain.api_identifier else "not supported"
        lines.append(f"{icon} <b>{chain.name}</b> — <code>{api_id}</code>")

    lines += ["", "⚠️ = Not in OpenSea API (skipped during polling)"]
    await (update.effective_message or update.message).reply_text("\n".join(lines), parse_mode="HTML")


# ── /filters ───────────────────────────────────────────────────────────────────

async def cmd_filters(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /filters — show current filter values (admin only)."""
    if not _admin_check(update, context):
        await _reject_unauthorized(update, context)
        return

    settings = context.bot_data.get("settings")
    if not settings:
        await (update.effective_message or update.message).reply_text("❌ Settings not available.")
        return

    interval_s = settings.poll_interval_seconds
    interval_m = interval_s / 60
    interval_str = f"{interval_m:.1f}m ({interval_s}s)" if interval_m >= 1 else f"{interval_s}s"

    lines = [
        "⚙️ <b>Current Filter & Polling Settings</b>",
        "",
        f"⏱ Fetch Interval:       <b>{interval_str}</b>",
        f"💵 Max Mint Price:      <b>${settings.max_mint_price_usd}</b>",
        f"📊 Offer Multiplier:    <b>{settings.min_offer_multiplier}×</b> (paid mints)",
        f"🆓 Free Mint Min Offer: <b>${settings.free_mint_min_offer_usd}</b>",
        f"🔥 Min Minted Supply:   <b>{settings.min_minted_percentage}%</b>",
        f"🛒 Allow Sold Out:      <b>{'Yes' if settings.allow_sold_out else 'No'}</b>",
        f"⏰ Notify Before:       <b>{settings.notification_before_minutes} min</b>",
        f"🗑 Delete After:        <b>{settings.delete_after_minutes} min</b>",
        "",
        "Commands to change settings:",
        "• <code>/setinterval &lt;time&gt;</code> (e.g. <code>30m</code>, <code>1h</code>, <code>45s</code>)",
        "• <code>/setfilter &lt;key&gt; &lt;value&gt;</code>",
        "  Keys: <code>max_mint_price_usd</code>, <code>min_offer_multiplier</code>,",
        "        <code>free_mint_min_offer_usd</code>, <code>min_minted_percentage</code>,",
        "        <code>allow_sold_out</code>, <code>delete_after_minutes</code>",
    ]
    await (update.effective_message or update.message).reply_text("\n".join(lines), parse_mode="HTML")


def _persist_to_env(env_var: str, value: str) -> bool:
    """Update or append an environment variable in the .env file."""
    env_file = Path(".env")
    if not env_file.exists():
        return False
    try:
        content = env_file.read_text(encoding="utf-8")
        lines = content.splitlines()
        found = False
        new_lines = []
        for line in lines:
            trimmed = line.strip()
            if trimmed.startswith(f"{env_var}=") or trimmed.startswith(f"{env_var} ="):
                new_lines.append(f"{env_var}={value}")
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(f"{env_var}={value}")
        env_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        return True
    except Exception as exc:
        log.warning("[CONFIG] Failed to write to .env: %s", exc)
        return False


# ── /setfilter ─────────────────────────────────────────────────────────────────

async def cmd_setfilter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /setfilter <key> <value> — change a filter value at runtime (admin only).

    Example:
      /setfilter min_minted_percentage 60
      /setfilter max_mint_price_usd 15
    """
    if not _admin_check(update, context):
        await _reject_unauthorized(update, context)
        return

    args = context.args
    if len(args) < 2:
        await (update.effective_message or update.message).reply_text(
            "Usage: /setfilter &lt;key&gt; &lt;value&gt;\n\n"
            "Valid keys:\n" +
            "\n".join(f"• <code>{k}</code> — {v[1]}" for k, v in FILTER_FIELDS.items()),
            parse_mode="HTML",
        )
        return

    key = args[0].lower()
    raw_value = args[1]

    if key not in FILTER_FIELDS:
        await (update.effective_message or update.message).reply_text(
            f"❌ Unknown key: <code>{key}</code>\n"
            f"Valid keys: {', '.join(f'<code>{k}</code>' for k in FILTER_FIELDS)}",
            parse_mode="HTML",
        )
        return

    if key == "allow_sold_out":
        value = raw_value.strip().lower() in ("true", "1", "yes", "on")
    elif key in ("poll_interval_seconds", "delete_after_minutes", "notification_before_minutes"):
        try:
            value = int(raw_value)
            if key == "poll_interval_seconds" and not (30 <= value <= 86400):
                raise ValueError("Must be between 30 and 86400 seconds")
            if key in ("delete_after_minutes", "notification_before_minutes") and value < 0:
                raise ValueError("Must be non-negative")
        except ValueError as exc:
            await (update.effective_message or update.message).reply_text(f"❌ Invalid value: {exc}")
            return
    else:
        try:
            value = Decimal(raw_value)
            if value < 0:
                raise ValueError("Value must be non-negative")
        except (InvalidOperation, ValueError) as exc:
            await (update.effective_message or update.message).reply_text(f"❌ Invalid value: {exc}")
            return

    settings = context.bot_data.get("settings")
    if settings:
        setattr(settings, key, value)
        env_var = FILTER_FIELDS[key][0]
        saved_env = _persist_to_env(env_var, str(value))
        note = "✅ Saved to .env — will persist across restarts." if saved_env else "⚡ Live updated for current session."
        await (update.effective_message or update.message).reply_text(
            f"✅ Updated <code>{key}</code> = <b>{value}</b>\n"
            f"<i>{note}</i>",
            parse_mode="HTML",
        )
        log.info("[CONFIG] Runtime filter change: %s = %s by user %s (saved_to_env=%s)", key, value, update.effective_user.id, saved_env)
    else:
        await (update.effective_message or update.message).reply_text("❌ Settings not available.")


# ── /setinterval ───────────────────────────────────────────────────────────────

async def cmd_setinterval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /setinterval <time> — change the fetching/scanning interval (admin only).

    Accepts:
      /setinterval 30m   — 30 minutes
      /setinterval 1h    — 1 hour
      /setinterval 45s   — 45 seconds
      /setinterval 1800  — seconds directly
    """
    if not _admin_check(update, context):
        await _reject_unauthorized(update, context)
        return

    settings = context.bot_data.get("settings")
    if not settings:
        await (update.effective_message or update.message).reply_text("❌ Settings not available.")
        return

    args = context.args
    if not args:
        interval_s = settings.poll_interval_seconds
        interval_m = interval_s / 60
        curr_str = f"{interval_m:.1f}m ({interval_s}s)" if interval_m >= 1 else f"{interval_s}s"
        await (update.effective_message or update.message).reply_text(
            f"⏱ Current Fetch Interval: <b>{curr_str}</b>\n\n"
            "<b>Usage:</b> <code>/setinterval &lt;time&gt;</code>\n\n"
            "<b>Examples:</b>\n"
            "• <code>/setinterval 30m</code> — Fetch every 30 minutes\n"
            "• <code>/setinterval 1h</code>  — Fetch every 1 hour\n"
            "• <code>/setinterval 15m</code> — Fetch every 15 minutes\n"
            "• <code>/setinterval 60s</code> — Fetch every 60 seconds",
            parse_mode="HTML",
        )
        return

    raw = args[0].strip().lower()
    try:
        if raw.endswith("h"):
            seconds = int(float(raw[:-1]) * 3600)
        elif raw.endswith("m"):
            seconds = int(float(raw[:-1]) * 60)
        elif raw.endswith("s"):
            seconds = int(float(raw[:-1]))
        else:
            seconds = int(raw)
    except (ValueError, TypeError):
        await (update.effective_message or update.message).reply_text("❌ Invalid format. Examples: <code>30m</code>, <code>1h</code>, <code>45s</code>, <code>1800</code>", parse_mode="HTML")
        return

    if not (30 <= seconds <= 86400):
        await (update.effective_message or update.message).reply_text("❌ Interval must be between 30 seconds and 86400 seconds (24 hours).")
        return

    settings.poll_interval_seconds = seconds
    saved_env = _persist_to_env("POLL_INTERVAL_SECONDS", str(seconds))
    note = "✅ Saved to .env — will persist across restarts." if saved_env else "⚡ Live updated for current session."

    mins = seconds / 60
    time_str = f"{mins:.1f} minutes ({seconds}s)" if mins >= 1 else f"{seconds} seconds"
    await (update.effective_message or update.message).reply_text(
        f"⏱ Fetch interval updated to <b>{time_str}</b>\n"
        f"<i>{note}</i>",
        parse_mode="HTML",
    )
    log.info("[CONFIG] Fetch interval updated to %ds by user %s (saved_to_env=%s)", seconds, update.effective_user.id, saved_env)


# ── /stats ─────────────────────────────────────────────────────────────────────

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /stats — today's discovery stats from the database (admin only)."""
    if not _admin_check(update, context):
        await _reject_unauthorized(update, context)
        return

    try:
        from sqlalchemy import func, select
        from datetime import datetime, timezone, timedelta
        from app.database.database import get_session_factory
        from app.database.models import MintOpportunityDB

        factory = get_session_factory()
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

        async with factory() as session:
            result = await session.execute(
                select(
                    MintOpportunityDB.status,
                    func.count(MintOpportunityDB.id).label("count"),
                ).where(
                    MintOpportunityDB.created_at >= today_start
                ).group_by(MintOpportunityDB.status)
            )
            rows = result.all()

        stats = {row.status: row.count for row in rows}
        total = sum(stats.values())

        lines = [
            "📊 <b>Today's Mint Stats</b>",
            f"<i>Since {today_start.strftime('%Y-%m-%d 00:00 UTC')}</i>",
            "",
            f"🔍 Discovered:  <b>{total}</b>",
            f"❌ Rejected:    <b>{stats.get('REJECTED', 0)}</b>",
            f"📅 Scheduled:   <b>{stats.get('SCHEDULED', 0)}</b>",
            f"✅ Notified:    <b>{stats.get('NOTIFIED', 0)}</b>",
            f"🗑 Deleted:     <b>{stats.get('DELETED', 0)}</b>",
            f"⏰ Expired:     <b>{stats.get('EXPIRED', 0)}</b>",
            f"⚠️ Failed:      <b>{stats.get('FAILED', 0)}</b>",
        ]
        await (update.effective_message or update.message).reply_text("\n".join(lines), parse_mode="HTML")

    except Exception as exc:
        log.error("[BOT] cmd_stats error: %s", exc)
        await (update.effective_message or update.message).reply_text(f"❌ Error fetching stats: {exc}")


# ── /recent ────────────────────────────────────────────────────────────────────

async def cmd_recent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /recent — show last 5 qualified mints (admin only)."""
    if not _admin_check(update, context):
        await _reject_unauthorized(update, context)
        return

    try:
        from sqlalchemy import select
        from app.database.database import get_session_factory
        from app.database.models import MintOpportunityDB

        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(MintOpportunityDB)
                .where(MintOpportunityDB.status.notin_(["REJECTED", "EXPIRED"]))
                .order_by(MintOpportunityDB.created_at.desc())
                .limit(5)
            )
            records = list(result.scalars().all())

        if not records:
            await (update.effective_message or update.message).reply_text("📭 No qualified mints found yet.")
            return

        lines = ["🕐 <b>Last 5 Qualified Mints</b>", ""]
        for r in records:
            status_icon = {
                "NOTIFIED": "🔔", "DELETED": "🗑", "SCHEDULED": "📅",
                "FAILED": "❌", "DISCOVERED": "🔍",
            }.get(r.status, "❓")
            lines.append(
                f"{status_icon} <b>{r.project_name}</b> ({r.chain})\n"
                f"   Price: ${r.mint_price_usd} | Offer: ${r.offer_price_usd or 'N/A'} | "
                f"Minted: {r.minted_percentage or 'N/A'}%\n"
                f"   Status: <code>{r.status}</code>"
            )

        await (update.effective_message or update.message).reply_text("\n\n".join(lines), parse_mode="HTML")

    except Exception as exc:
        log.error("[BOT] cmd_recent error: %s", exc)
        await (update.effective_message or update.message).reply_text(f"❌ Error: {exc}")


# ── /jobs ──────────────────────────────────────────────────────────────────────

async def cmd_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /jobs — show all pending APScheduler jobs (admin only)."""
    if not _admin_check(update, context):
        await _reject_unauthorized(update, context)
        return

    monitor: MintMonitor | None = context.bot_data.get("monitor")
    if monitor is None:
        await (update.effective_message or update.message).reply_text("❌ Monitor not initialised.")
        return

    jobs = monitor._scheduler.list_jobs()
    if not jobs:
        await (update.effective_message or update.message).reply_text("📭 No pending scheduled jobs.")
        return

    lines = [f"⏰ <b>Pending Jobs ({len(jobs)})</b>", ""]
    for job in jobs[:15]:  # Cap at 15 for readability
        job_type = "🔔 Notify" if job["id"].startswith("notify_") else "🗑 Delete"
        mint_id_short = job["id"].split("_", 1)[1][:8]
        run_at = job.get("next_run_time", "unknown")
        lines.append(f"{job_type} <code>{mint_id_short}…</code> → <code>{run_at}</code>")

    if len(jobs) > 15:
        lines.append(f"\n<i>…and {len(jobs) - 15} more</i>")

    await (update.effective_message or update.message).reply_text("\n".join(lines), parse_mode="HTML")


# ── /test ──────────────────────────────────────────────────────────────────────

# ── /targets ───────────────────────────────────────────────────────────────────

async def cmd_targets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /targets — list all broadcast destination groups/channels/topics (admin only)."""
    if not _admin_check(update, context):
        await _reject_unauthorized(update, context)
        return

    settings = context.bot_data.get("settings")
    from app.database.database import get_session_factory
    from app.database.repository import TargetRepository

    factory = get_session_factory()
    async with factory() as session:
        extra_targets = await TargetRepository(session).get_all_active()

    lines = [
        "📢 <b>Broadcast Targets</b>",
        "Alerts are sent to all active destinations below:",
        "",
        "⭐ <b>Primary Target (.env)</b>",
        f"• Chat ID: <code>{settings.telegram_group_id if settings else 'N/A'}</code>",
        f"• Topic ID: <code>{settings.public_mint_topic_id if settings else 'N/A'}</code>",
        "",
    ]

    if extra_targets:
        lines.append(f"➕ <b>Additional Destinations ({len(extra_targets)})</b>")
        for i, t in enumerate(extra_targets, 1):
            label_str = f" ({t.label})" if t.label else ""
            topic_str = f"Topic: <code>{t.topic_id}</code>" if t.topic_id else "<i>General / Channel</i>"
            lines.append(f"{i}. <b>Chat:</b> <code>{t.chat_id}</code> | {topic_str}{label_str}")
    else:
        lines.append("<i>No additional targets added yet.</i>")

    lines += [
        "",
        "<b>Commands to manage targets:</b>",
        "• <code>/addtarget &lt;chat_id&gt; [topic_id] [label]</code>",
        "• <code>/removetarget &lt;chat_id&gt; [topic_id]</code>",
        "• <code>/test</code> — sends a test alert to all targets",
    ]
    await (update.effective_message or update.message).reply_text("\n".join(lines), parse_mode="HTML")


# ── /addtarget ─────────────────────────────────────────────────────────────────

async def cmd_addtarget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /addtarget <chat_id> [topic_id] [label] (admin only).
    Adds a new destination group or channel.
    """
    if not _admin_check(update, context):
        await _reject_unauthorized(update, context)
        return

    args = context.args
    if not args:
        await (update.effective_message or update.message).reply_text(
            "<b>Usage:</b>\n"
            "<code>/addtarget &lt;chat_id&gt; [topic_id] [label]</code>\n\n"
            "<b>Examples:</b>\n"
            "• Group with topic: <code>/addtarget -1001234567890 42 \"VIP Mints\"</code>\n"
            "• Channel or regular group: <code>/addtarget -1009876543210 0 \"Alpha Channel\"</code>\n"
            "• Without label: <code>/addtarget -1001234567890 156</code>\n\n"
            "<i>Note: Make sure the bot is added as an admin in the target group/channel.</i>",
            parse_mode="HTML",
        )
        return

    try:
        chat_id = int(args[0])
    except ValueError:
        await (update.effective_message or update.message).reply_text("❌ Invalid chat_id. It must be an integer (e.g. <code>-1001234567890</code>).", parse_mode="HTML")
        return

    topic_id: Optional[int] = None
    label: Optional[str] = None

    if len(args) >= 2:
        try:
            val = int(args[1])
            topic_id = val if val > 0 else None
            if len(args) >= 3:
                label = " ".join(args[2:]).strip("\"'")
        except ValueError:
            label = " ".join(args[1:]).strip("\"'")

    from app.database.database import get_session_factory
    from app.database.repository import TargetRepository

    factory = get_session_factory()
    async with factory() as session:
        await TargetRepository(session).add_target(
            chat_id=chat_id, topic_id=topic_id, label=label
        )
        await session.commit()

    topic_desc = f"<code>{topic_id}</code>" if topic_id else "<i>General / Channel (No topic)</i>"
    label_desc = f"<b>{label}</b>" if label else "<i>None</i>"

    await (update.effective_message or update.message).reply_text(
        "✅ <b>Broadcast target added!</b>\n\n"
        f"• <b>Chat ID:</b> <code>{chat_id}</code>\n"
        f"• <b>Topic ID:</b> {topic_desc}\n"
        f"• <b>Label:</b> {label_desc}\n\n"
        "<i>All future qualified mint alerts will now be sent to this destination.</i>\n"
        "<i>Tip: Run <code>/test</code> to send a test alert.</i>",
        parse_mode="HTML",
    )
    log.info("[TARGET_CONFIG] Target added chat_id=%d topic_id=%s label=%s by user %s", chat_id, topic_id, label, update.effective_user.id)


# ── /removetarget ──────────────────────────────────────────────────────────────

async def cmd_removetarget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /removetarget <chat_id> [topic_id] (admin only).
    """
    if not _admin_check(update, context):
        await _reject_unauthorized(update, context)
        return

    args = context.args
    if not args:
        await (update.effective_message or update.message).reply_text(
            "<b>Usage:</b>\n"
            "<code>/removetarget &lt;chat_id&gt; [topic_id]</code>\n\n"
            "Run <code>/targets</code> to see active targets.",
            parse_mode="HTML",
        )
        return

    try:
        chat_id = int(args[0])
    except ValueError:
        await (update.effective_message or update.message).reply_text("❌ Invalid chat_id. It must be an integer.", parse_mode="HTML")
        return

    topic_id: Optional[int] = None
    if len(args) >= 2:
        try:
            val = int(args[1])
            topic_id = val if val > 0 else None
        except ValueError:
            pass

    from app.database.database import get_session_factory
    from app.database.repository import TargetRepository

    factory = get_session_factory()
    async with factory() as session:
        removed = await TargetRepository(session).remove_target(chat_id=chat_id, topic_id=topic_id)
        await session.commit()

    if removed:
        await (update.effective_message or update.message).reply_text(
            f"✅ Removed target <code>{chat_id}</code> (topic: <code>{topic_id or 'General'}</code>).",
            parse_mode="HTML",
        )
        log.info("[TARGET_CONFIG] Target removed chat_id=%d topic_id=%s by user %s", chat_id, topic_id, update.effective_user.id)
    else:
        await (update.effective_message or update.message).reply_text(
            f"⚠️ Target <code>{chat_id}</code> (topic: <code>{topic_id or 'General'}</code>) was not found in additional targets list.",
            parse_mode="HTML",
        )


# ── /test ──────────────────────────────────────────────────────────────────────

async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /test — send a mock alert to all broadcast targets (admin only)."""
    if not _admin_check(update, context):
        await _reject_unauthorized(update, context)
        return

    settings = context.bot_data.get("settings")
    if not settings:
        await (update.effective_message or update.message).reply_text("❌ Settings not available.")
        return

    test_text = (
        "🚨 <b>PUBLIC MINT ALERT</b> [TEST]\n\n"
        "<b>Project:</b> Example Collection\n"
        "<b>Chain:</b> Base\n\n"
        "<b>Mint Price:</b> $10.00 (0.0037 ETH)\n"
        "<b>Offer Price:</b> $16.00\n"
        "<b>Minted:</b> 82.00%\n\n"
        "<b>Starts in:</b> 10 minutes\n\n"
        "🔗 <b>Mint:</b> <a href=\"https://opensea.io/collection/example\">Direct Link</a>\n"
        "📄 <b>Contract:</b> <code>0x0000000000000000000000000000000000000001</code>\n\n"
        "<i>Test message — not a real mint.</i>"
    )

    from app.database.database import get_session_factory
    from app.database.repository import TargetRepository

    factory = get_session_factory()
    async with factory() as session:
        extra_targets = await TargetRepository(session).get_all_active()

    targets: list[tuple[int, Optional[int], str]] = [
        (settings.telegram_group_id, settings.public_mint_topic_id, "Primary (.env)")
    ]
    seen = {(settings.telegram_group_id, settings.public_mint_topic_id)}
    for t in extra_targets:
        key = (t.chat_id, t.topic_id)
        if key not in seen:
            seen.add(key)
            name = t.label or f"Chat {t.chat_id}"
            targets.append((t.chat_id, t.topic_id, name))

    results = []
    for chat_id, topic_id, label in targets:
        try:
            kwargs = {
                "chat_id": chat_id,
                "text": test_text,
                "parse_mode": "HTML",
            }
            if topic_id:
                kwargs["message_thread_id"] = topic_id
            msg = await context.bot.send_message(**kwargs)
            results.append(f"✅ <b>{label}</b> (<code>{chat_id}</code>): message_id=<code>{msg.message_id}</code>")
        except Exception as exc:
            results.append(f"❌ <b>{label}</b> (<code>{chat_id}</code>): {exc}")

    lines = [
        "🧪 <b>Test Alert Broadcast Results</b>",
        "",
    ] + results
    await (update.effective_message or update.message).reply_text("\n".join(lines), parse_mode="HTML")


# ── /admins ────────────────────────────────────────────────────────────────────

async def cmd_admins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /admins — list all current admins (admin only)."""
    if not _admin_check(update, context):
        await _reject_unauthorized(update, context)
        return

    admin_ids: list[int] = context.bot_data.get("admin_ids", [])
    if not admin_ids:
        await (update.effective_message or update.message).reply_text("ℹ️ No admin restriction set — all users can use admin commands.")
        return

    lines = ["👑 <b>Current Admins</b>", ""]
    for i, uid in enumerate(admin_ids):
        label = " <b>(super-admin)</b>" if i == 0 else ""
        lines.append(f"• <code>{uid}</code>{label}")

    lines += [
        "",
        "Super-admin can add/remove admins with:",
        "/addadmin &lt;user_id&gt;",
        "/removeadmin &lt;user_id&gt;",
    ]
    await (update.effective_message or update.message).reply_text("\n".join(lines), parse_mode="HTML")


# ── /addadmin ──────────────────────────────────────────────────────────────────

async def cmd_addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /addadmin <user_id> — add a new admin (super-admin only).

    How to get a user's ID:
      Ask them to message @userinfobot — it replies with their Telegram user ID.

    Example:
      /addadmin 987654321
    """
    admin_ids: list[int] = context.bot_data.get("admin_ids", [])
    user_id = update.effective_user.id

    # Only the super-admin (first in list) can add new admins
    if not _is_super_admin(user_id, admin_ids):
        if not _admin_check(update, context):
            await _reject_unauthorized(update, context)
        else:
            await (update.effective_message or update.message).reply_text(
                "⛔ Only the <b>super-admin</b> can add new admins.\n"
                f"Super-admin ID: <code>{admin_ids[0] if admin_ids else 'none'}</code>",
                parse_mode="HTML",
            )
        return

    args = context.args
    if not args:
        await (update.effective_message or update.message).reply_text(
            "Usage: /addadmin &lt;user_id&gt;\n\n"
            "To get someone's user ID, ask them to message @userinfobot.",
            parse_mode="HTML",
        )
        return

    try:
        new_id = int(args[0])
    except ValueError:
        await (update.effective_message or update.message).reply_text("❌ Invalid user ID — must be a number.\nExample: /addadmin 987654321")
        return

    if new_id in admin_ids:
        await (update.effective_message or update.message).reply_text(f"ℹ️ User <code>{new_id}</code> is already an admin.", parse_mode="HTML")
        return

    admin_ids.append(new_id)
    context.bot_data["admin_ids"] = admin_ids
    await _persist_admin_ids(context, admin_ids)

    log.info("[ADMIN] Super-admin %d added new admin: %d", user_id, new_id)
    await (update.effective_message or update.message).reply_text(
        f"✅ <code>{new_id}</code> has been added as an admin.\n\n"
        f"They now have access to all admin commands.\n"
        f"Total admins: <b>{len(admin_ids)}</b>",
        parse_mode="HTML",
    )


# ── /removeadmin ───────────────────────────────────────────────────────────────

async def cmd_removeadmin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /removeadmin <user_id> — remove an admin (super-admin only).

    The super-admin (first ID in list) cannot remove themselves.

    Example:
      /removeadmin 987654321
    """
    admin_ids: list[int] = context.bot_data.get("admin_ids", [])
    user_id = update.effective_user.id

    if not _is_super_admin(user_id, admin_ids):
        if not _admin_check(update, context):
            await _reject_unauthorized(update, context)
        else:
            await (update.effective_message or update.message).reply_text(
                "⛔ Only the <b>super-admin</b> can remove admins.",
                parse_mode="HTML",
            )
        return

    args = context.args
    if not args:
        await (update.effective_message or update.message).reply_text("Usage: /removeadmin &lt;user_id&gt;", parse_mode="HTML")
        return

    try:
        target_id = int(args[0])
    except ValueError:
        await (update.effective_message or update.message).reply_text("❌ Invalid user ID — must be a number.")
        return

    if target_id == admin_ids[0]:
        await (update.effective_message or update.message).reply_text(
            "⛔ You cannot remove yourself (super-admin).\n"
            "Transfer super-admin role first by editing <code>ADMIN_USER_IDS</code> in .env.",
            parse_mode="HTML",
        )
        return

    if target_id not in admin_ids:
        await (update.effective_message or update.message).reply_text(f"ℹ️ <code>{target_id}</code> is not an admin.", parse_mode="HTML")
        return

    admin_ids.remove(target_id)
    context.bot_data["admin_ids"] = admin_ids
    await _persist_admin_ids(context, admin_ids)

    log.info("[ADMIN] Super-admin %d removed admin: %d", user_id, target_id)
    await (update.effective_message or update.message).reply_text(
        f"✅ <code>{target_id}</code> has been removed from admins.\n"
        f"Remaining admins: <b>{len(admin_ids)}</b>",
        parse_mode="HTML",
    )

