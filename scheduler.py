"""Automatic membership-expiry processing."""

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from config import CHANNEL_ID
from database import mark_expired, memberships_due_for_expiry

logger = logging.getLogger(__name__)


async def expire_due_memberships(bot: Bot) -> None:
    """Remove due members from the channel and persist their expired state."""
    for user in await memberships_due_for_expiry():
        user_id = user[0]
        try:
            await bot.ban_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
            await bot.unban_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
            await mark_expired(user_id)
            logger.info("Expired membership removed: user_id=%s channel_id=%s", user_id, CHANNEL_ID)
        except TelegramAPIError:
            logger.exception("Could not remove expired member: user_id=%s", user_id)


async def run_expiry_scheduler(bot: Bot) -> None:
    """Process expirations once per hour until cancelled."""
    while True:
        await expire_due_memberships(bot)
        await asyncio.sleep(3600)
