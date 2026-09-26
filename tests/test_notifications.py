"""
Tests for Telegram notification formatting and delivery.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.database.models import MintOpportunityDB
from app.services.notification_service import TelegramNotificationService, _format_alert, _esc


def make_mint_db(
    project_name: str = "Test Project",
    chain: str = "ethereum",
    mint_price_usd: str = "10.00",
    offer_price_usd: str = "20.00",
    minted_percentage: str = "75.00",
    mint_start_offset_minutes: int = 10,
) -> MintOpportunityDB:
    return MintOpportunityDB(
        id=str(uuid.uuid4()),
        provider="opensea",
        external_id=str(uuid.uuid4()),
        collection_slug="test",
        project_name=project_name,
        chain=chain,
        contract_address="0xTEST123",
        mint_type="public_sale",
        mint_price_usd=mint_price_usd,
        offer_price_usd=offer_price_usd,
        minted_percentage=minted_percentage,
        total_supply=1000,
        minted_quantity=750,
        mint_start_time=datetime.now(timezone.utc) + timedelta(minutes=mint_start_offset_minutes),
        mint_url="https://opensea.io/collection/test",
        status="SCHEDULED",
    )


@pytest.mark.asyncio
async def test_send_alert_returns_message_id():
    """send_mint_alert returns the Telegram message_id on success."""
    bot = AsyncMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=98765))
    notifier = TelegramNotificationService(bot=bot, chat_id=-100111, topic_id=42)

    mint_db = make_mint_db()
    msg_id = await notifier.send_mint_alert(mint_db)

    assert msg_id == 98765


@pytest.mark.asyncio
async def test_send_alert_uses_topic_id():
    """send_mint_alert always uses message_thread_id = topic_id."""
    bot = AsyncMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
    notifier = TelegramNotificationService(bot=bot, chat_id=-100999, topic_id=55)

    mint_db = make_mint_db()
    await notifier.send_mint_alert(mint_db)

    call_kwargs = bot.send_message.call_args.kwargs
    assert call_kwargs["message_thread_id"] == 55
    assert call_kwargs["chat_id"] == -100999


@pytest.mark.asyncio
async def test_send_alert_html_parse_mode():
    """send_mint_alert uses HTML parse mode."""
    bot = AsyncMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
    notifier = TelegramNotificationService(bot=bot, chat_id=-100, topic_id=1)

    mint_db = make_mint_db()
    await notifier.send_mint_alert(mint_db)

    call_kwargs = bot.send_message.call_args.kwargs
    assert call_kwargs["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_send_alert_returns_none_on_error():
    """send_mint_alert returns None when Telegram raises an error."""
    from telegram.error import TelegramError

    bot = AsyncMock()
    bot.send_message = AsyncMock(side_effect=TelegramError("Forbidden"))
    notifier = TelegramNotificationService(bot=bot, chat_id=-100, topic_id=1)

    mint_db = make_mint_db()
    result = await notifier.send_mint_alert(mint_db)

    assert result is None


@pytest.mark.asyncio
async def test_delete_alert_success():
    """delete_mint_alert returns True on success."""
    bot = AsyncMock()
    bot.delete_message = AsyncMock(return_value=True)
    notifier = TelegramNotificationService(bot=bot, chat_id=-100, topic_id=1)

    result = await notifier.delete_mint_alert(message_id=12345, mint_id="test")
    assert result is True


@pytest.mark.asyncio
async def test_delete_alert_failure():
    """delete_mint_alert returns False on error — does not raise."""
    from telegram.error import TelegramError

    bot = AsyncMock()
    bot.delete_message = AsyncMock(side_effect=TelegramError("Message not found"))
    notifier = TelegramNotificationService(bot=bot, chat_id=-100, topic_id=1)

    result = await notifier.delete_mint_alert(message_id=99999, mint_id="test")
    assert result is False


def test_format_alert_contains_required_fields():
    """Alert message contains all required display fields."""
    mint_db = make_mint_db(
        project_name="Cool Collection",
        chain="base",
        mint_price_usd="5.00",
        offer_price_usd="10.00",
        minted_percentage="80.00",
    )
    text = _format_alert(mint_db)

    assert "Cool Collection" in text
    assert "5.00" in text
    assert "10.00" in text
    assert "80.00" in text
    assert "PUBLIC MINT ALERT" in text
    assert "Mint:" in text
    assert "Contract:" in text


def test_format_alert_escapes_html():
    """Dynamic content containing HTML special chars is properly escaped."""
    mint_db = make_mint_db(project_name="<script>alert('xss')</script>")
    text = _format_alert(mint_db)

    # The raw script tag should NOT appear unescaped
    assert "<script>" not in text
    assert "&lt;script&gt;" in text


def test_esc_none_returns_na():
    """_esc(None) returns 'N/A'."""
    assert _esc(None) == "N/A"


def test_esc_empty_returns_na():
    """_esc('') returns 'N/A'."""
    assert _esc("") == "N/A"


def test_esc_special_chars():
    """HTML special characters are escaped."""
    result = _esc("<b>Bold & Cool</b>")
    assert "&lt;" in result
    assert "&amp;" in result
