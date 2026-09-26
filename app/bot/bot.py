"""
Telegram bot initialisation and command registration.
"""
from __future__ import annotations

import logging

from telegram import BotCommand
from telegram.ext import Application, CommandHandler

from app.bot.handlers import (
    cmd_addadmin,
    cmd_addtarget,
    cmd_admins,
    cmd_chains,
    cmd_filters,
    cmd_help,
    cmd_jobs,
    cmd_monitor,
    cmd_ping,
    cmd_recent,
    cmd_removeadmin,
    cmd_removetarget,
    cmd_setfilter,
    cmd_setinterval,
    cmd_start,
    cmd_stats,
    cmd_status,
    cmd_targets,
    cmd_test,
)

log = logging.getLogger(__name__)

BOT_COMMANDS = [
    BotCommand("start",        "Welcome message"),
    BotCommand("help",         "List all commands"),
    BotCommand("ping",         "Check if bot is alive"),
    BotCommand("status",       "Bot health and live metrics"),
    BotCommand("monitor",      "Enable or pause monitoring (on/off)"),
    BotCommand("chains",       "Show all monitored chains"),
    BotCommand("filters",      "Show current filter settings"),
    BotCommand("setfilter",    "Change a filter value at runtime"),
    BotCommand("setinterval",  "Change fetch interval (e.g. 30m, 1h)"),
    BotCommand("targets",      "List all broadcast destinations"),
    BotCommand("addtarget",    "Add a destination group or channel"),
    BotCommand("removetarget", "Remove a destination group or channel"),
    BotCommand("stats",        "Today's discovery stats"),
    BotCommand("recent",       "Last 5 qualified mints"),
    BotCommand("jobs",         "Show pending scheduled jobs"),
    BotCommand("admins",       "List all current admins"),
    BotCommand("addadmin",     "Add a new admin (super-admin only)"),
    BotCommand("removeadmin",  "Remove an admin (super-admin only)"),
    BotCommand("test",         "Send a test alert to all targets"),
]


def build_application(token: str) -> Application:
    """
    Build and configure the python-telegram-bot Application.

    Registers all command handlers and sets the bot command menu.
    """
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start",        cmd_start))
    app.add_handler(CommandHandler("help",         cmd_help))
    app.add_handler(CommandHandler("ping",         cmd_ping))
    app.add_handler(CommandHandler("status",       cmd_status))
    app.add_handler(CommandHandler("monitor",      cmd_monitor))
    app.add_handler(CommandHandler("chains",       cmd_chains))
    app.add_handler(CommandHandler("filters",      cmd_filters))
    app.add_handler(CommandHandler("setfilter",    cmd_setfilter))
    app.add_handler(CommandHandler("setinterval",  cmd_setinterval))
    app.add_handler(CommandHandler("targets",      cmd_targets))
    app.add_handler(CommandHandler("addtarget",    cmd_addtarget))
    app.add_handler(CommandHandler("removetarget", cmd_removetarget))
    app.add_handler(CommandHandler("stats",        cmd_stats))
    app.add_handler(CommandHandler("recent",       cmd_recent))
    app.add_handler(CommandHandler("jobs",         cmd_jobs))
    app.add_handler(CommandHandler("admins",       cmd_admins))
    app.add_handler(CommandHandler("addadmin",     cmd_addadmin))
    app.add_handler(CommandHandler("removeadmin",  cmd_removeadmin))
    app.add_handler(CommandHandler("test",         cmd_test))

    log.info("[BOT] Telegram application built — %d handlers registered", len(BOT_COMMANDS))
    return app
