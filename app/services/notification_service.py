"""
Telegram notification service.

Sends public mint alerts to the configured group + topic.
Deletes a specific message by ID when deletion time arrives.
Safely escapes all dynamic content for HTML formatting.

Telegram format: HTML (parse_mode="HTML")
Topic targeting: message_thread_id parameter
"""
from __future__ import annotations

import html
import logging
from decimal import Decimal
from typing import Optional

from telegram import Bot
from telegram.error import TelegramError

from app.database.models import MintOpportunityDB
from app.utils.time import format_time_remaining, minutes_until

log = logging.getLogger(__name__)

# Native zero-address: payment is in the chain's native token
NATIVE_ZERO = "0x0000000000000000000000000000000000000000"


class TelegramNotificationService:
    """Sends and deletes mint alert messages in a Telegram forum topic."""

    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        topic_id: int,
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._topic_id = topic_id

    async def send_mint_alert(
        self, mint: MintOpportunityDB
    ) -> Optional[int]:
        """
        Send a public mint alert to the configured topic.

        Returns the Telegram message_id on success, None on failure.
        """
        text = _format_alert(mint)
        try:
            msg = await self._bot.send_message(
                chat_id=self._chat_id,
                message_thread_id=self._topic_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=False,
            )
            log.info(
                "[TELEGRAM_SENT] mint_id=%s message_id=%d chat_id=%d thread_id=%d",
                mint.id,
                msg.message_id,
                self._chat_id,
                self._topic_id,
            )
            return msg.message_id
        except TelegramError as exc:
            log.error(
                "[TELEGRAM_ERROR] Failed to send alert mint_id=%s error=%s",
                mint.id,
                exc,
            )
            return None

    async def delete_mint_alert(
        self, message_id: int, mint_id: str
    ) -> bool:
        """
        Delete a specific message from the topic.

        Returns True on success, False on failure.
        Only deletes the exact message stored against this mint.
        """
        try:
            await self._bot.delete_message(
                chat_id=self._chat_id,
                message_id=message_id,
            )
            log.info(
                "[TELEGRAM_DELETE] mint_id=%s message_id=%d",
                mint_id,
                message_id,
            )
            return True
        except TelegramError as exc:
            log.warning(
                "[TELEGRAM_ERROR] Failed to delete message_id=%d mint_id=%s error=%s",
                message_id,
                mint_id,
                exc,
            )
            return False


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------

def _format_alert(mint: MintOpportunityDB) -> str:
    """
    Format a mint alert as Telegram HTML.

    All dynamic values are HTML-escaped to prevent injection/formatting breaks.
    """
    project = _esc(mint.project_name)
    chain = _esc(mint.chain.title())

    mint_price_str = _format_price(mint.mint_price_usd)
    if mint.mint_price_eth and mint.mint_price_eth not in ("0", "0.0", "None"):
        try:
            eth_val = Decimal(str(mint.mint_price_eth))
            if eth_val > 0:
                mint_price_str = f"{mint_price_str} ({eth_val:f} ETH)"
        except Exception:
            pass

    offer = _format_price(mint.offer_price_usd) if mint.offer_price_usd else "N/A"
    minted_pct = f"{mint.minted_percentage}%" if mint.minted_percentage else "N/A"
    mint_url = _esc(mint.mint_url)
    contract = _esc(mint.contract_address)

    # Calculate "Starts in"
    if mint.mint_start_time:
        mins = minutes_until(mint.mint_start_time)
        starts_in = format_time_remaining(mins) if mins > 0 else "Live Now"
    else:
        starts_in = "Unknown"

    return (
        "🚨 <b>PUBLIC MINT ALERT</b>\n\n"
        f"<b>Project:</b> {project}\n"
        f"<b>Chain:</b> {chain}\n\n"
        f"<b>Mint Price:</b> {mint_price_str}\n"
        f"<b>Offer Price:</b> {offer}\n"
        f"<b>Minted:</b> {minted_pct}\n\n"
        f"<b>Starts in:</b> {_esc(starts_in)}\n\n"
        f"🔗 <b>Mint:</b> <a href=\"{mint_url}\">Direct Link</a>\n"
        f"📄 <b>Contract:</b> <code>{contract}</code>"
    )


def _esc(value: str | None) -> str:
    """HTML-escape a dynamic string value."""
    if not value:
        return "N/A"
    return html.escape(str(value))


def _format_price(value: str | None) -> str:
    """Format a USD price string."""
    if not value:
        return "$0.00"
    try:
        d = Decimal(str(value))
        return f"${d:.2f}"
    except Exception:
        return f"${value}"
