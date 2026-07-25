"""Telegram VIP membership bot."""

import asyncio
import logging
from html import escape
from pathlib import Path
from typing import Final
from urllib.parse import urlencode

import qrcode
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramUnauthorizedError,
)
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    ChatJoinRequest,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import ADMIN_ID, BOT_TOKEN, CHANNEL_ID, UPI_ID, UPI_NAME
from database import (
    active_users,
    approve_membership,
    approved_users,
    datetime_from_storage,
    expired_users,
    extend_membership,
    get_user,
    init_db,
    mark_expired,
    membership_state,
    pending_users,
    rejected_users,
    save_user,
    save_join_request_link,
    total_users,
    update_status,
)
from scheduler import expire_due_memberships, run_expiry_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
@dp.channel_post()
async def debug_channel(message: Message):
    logger.info("=" * 50)
    logger.info("CHANNEL ID : %s", message.chat.id)
    logger.info("TITLE      : %s", message.chat.title)
    logger.info("TYPE       : %s", message.chat.type)
    logger.info("=" * 50)


QR_DIRECTORY: Final[Path] = Path("qr")
PLANS: Final[dict[str, tuple[str, int, int]]] = {
    "plan_7": ("7 Days", 99, 7),
    "plan_1": ("1 Month", 199, 30),
    "plan_3": ("3 Months", 499, 90),
    "plan_6": ("6 Months", 899, 180),
}


def plans_keyboard() -> InlineKeyboardMarkup:
    """Build the subscription plan selection keyboard."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="7 Days — ₹99", callback_data="plan_7")],
            [InlineKeyboardButton(text="1 Month — ₹199", callback_data="plan_1")],
            [InlineKeyboardButton(text="3 Months — ₹499", callback_data="plan_3")],
            [InlineKeyboardButton(text="6 Months — ₹899", callback_data="plan_6")],
        ]
    )


def generate_qr(user_id: int, amount: int) -> tuple[Path, str]:
    """Create a UPI QR code for a selected plan and return its path and note."""
    note = f"VIP_{user_id}"
    upi_url = "upi://pay?" + urlencode(
        {
            "pa": UPI_ID,
            "pn": UPI_NAME,
            "am": str(amount),
            "cu": "INR",
            "tn": note,
        }
    )
    QR_DIRECTORY.mkdir(parents=True, exist_ok=True)
    qr_path = QR_DIRECTORY / f"{user_id}_{amount}.png"
    qrcode.make(upi_url).save(qr_path)
    return qr_path, note


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


@dp.message(CommandStart())
async def start(message: Message) -> None:
    """Show available plans to a user."""
    await message.answer(
        "<b>🔥 VIP User Premium</b>\n\n"
        "Choose a subscription plan to receive your secure UPI payment QR code.",
        reply_markup=plans_keyboard(),
    )


@dp.callback_query(F.data.in_(PLANS.keys()))
async def choose_plan(callback: CallbackQuery) -> None:
    """Save a selected plan and send its UPI QR code."""
    if callback.message is None or callback.data is None:
        await callback.answer("Unable to process this selection.", show_alert=True)
        return

    plan_name, amount, days = PLANS[callback.data]
    user = callback.from_user
    qr_path, note = generate_qr(user.id, amount)
    await save_user(user.id, user.username, user.full_name, plan_name, amount, days)

    await callback.message.answer_photo(
        photo=FSInputFile(qr_path),
        caption=(
            f"<b>✅ {escape(plan_name)} Membership</b>\n\n"
            f"💰 <b>Amount:</b> ₹{amount}\n\n"
            "<b>How to pay</b>\n"
            "1. Scan this QR code, or pay to the UPI ID below.\n"
            f"2. UPI ID: <code>{escape(UPI_ID)}</code>\n"
            f"3. Payment note: <code>{note}</code>\n\n"
            "After payment, send the payment screenshot in this chat for verification."
        ),
    )
    await callback.answer("Plan selected")


@dp.message(F.photo)
async def receive_payment(message: Message) -> None:
    """Forward a payment screenshot to the admin for approval."""
    user = await get_user(message.from_user.id)
    if user is None:
        await message.answer("❌ Please choose a subscription plan before sending a screenshot.")
        return

    if user[5] == "Approved":
        await message.answer("✅ Your membership is already approved. Please check your messages.")
        return

    if user[5] == "Rejected":
        await message.answer("❌ This payment was rejected. Please choose a plan and submit a new payment.")
        return

    username = f"@{message.from_user.username}" if message.from_user.username else "Not set"
    caption = (
        "<b>💳 New Payment Verification Request</b>\n\n"
        f"<b>Name:</b> {escape(message.from_user.full_name)}\n"
        f"<b>User ID:</b> <code>{message.from_user.id}</code>\n"
        f"<b>Username:</b> {escape(username)}\n"
        f"<b>Plan:</b> {escape(str(user[3]))}\n"
        f"<b>Amount:</b> ₹{user[4]}"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Approve", callback_data=f"approve:{message.from_user.id}")],
            [InlineKeyboardButton(text="❌ Reject", callback_data=f"reject:{message.from_user.id}")],
        ]
    )

    try:
        await bot.send_photo(
            chat_id=ADMIN_ID,
            photo=message.photo[-1].file_id,
            caption=caption,
            reply_markup=keyboard,
        )
    except TelegramAPIError:
        logger.exception("Could not forward payment screenshot for user %s", message.from_user.id)
        await message.answer("❌ We could not submit your screenshot. Please try again shortly.")
        return

    await message.answer("✅ Screenshot received. Please wait for admin verification.")


async def process_admin_callback(callback: CallbackQuery) -> int | None:
    """Validate an approval/rejection callback and return its target user ID."""
    if not is_admin(callback.from_user.id):
        await callback.answer("This action is only available to the administrator.", show_alert=True)
        return None
    if callback.data is None or callback.message is None:
        await callback.answer("Invalid request.", show_alert=True)
        return None
    try:
        _, user_id_text = callback.data.split(":", maxsplit=1)
        return int(user_id_text)
    except ValueError:
        logger.warning("Invalid admin callback data: %r", callback.data)
        await callback.answer("Invalid request.", show_alert=True)
        return None


async def mark_admin_message(callback: CallbackQuery, status: str) -> None:
    """Append a final decision to the original admin screenshot caption."""
    if callback.message is None:
        return
    caption = callback.message.caption or "<b>Payment Verification Request</b>"
    try:
        await callback.message.edit_caption(caption=f"{caption}\n\n<b>{status}</b>")
    except TelegramAPIError:
        logger.exception("Could not update admin payment message")


@dp.callback_query(F.data.startswith("approve:"))
async def approve(callback: CallbackQuery) -> None:
    """Approve payment, issue a join-request link, and notify the user."""
    user_id = await process_admin_callback(callback)
    if user_id is None:
        return

    user = await get_user(user_id)
    if user is None or user[5] != "Pending":
        await callback.answer("This payment has already been processed.", show_alert=True)
        return

    logger.info("Admin %s is approving payment for user %s", callback.from_user.id, user_id)
    logger.info("Creating join-request invite link with chat_id=%s", CHANNEL_ID)
    try:
        invite = await bot.create_chat_invite_link(
            chat_id=CHANNEL_ID,
            creates_join_request=True,
        )
    except TelegramAPIError:
        logger.exception(
            "Failed to create join-request invite link: channel_id=%s, user_id=%s. "
            "Verify the channel ID and that the bot is a channel administrator with "
            "permission to invite users.",
            CHANNEL_ID,
            user_id,
        )
        await callback.answer(
            "Invite creation failed. Check the bot logs for the channel configuration error.",
            show_alert=True,
        )
        return

    try:
        join_keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🚀 Join Premium Channel", url=invite.invite_link)]
            ]
        )
        await save_join_request_link(user_id, invite.invite_link)
        expiry_date = await approve_membership(user_id)
        if expiry_date is None:
            logger.error("User %s disappeared before approval was saved", user_id)
            await callback.answer("Membership record no longer exists.", show_alert=True)
            return
        await bot.send_message(
            user_id,
            "<b>🎉 Payment Approved</b>\n\n"
            "Welcome to VIP Premium. Use the button below to request access to the "
            "private channel. Your request will be approved automatically while your "
            "membership is active.\n\n"
            f"Your membership expires on <b>{expiry_date:%d %b %Y %H:%M UTC}</b>.",
            reply_markup=join_keyboard,
        )
    except TelegramAPIError:
        logger.exception("Approval notification failed for user %s", user_id)
        await callback.answer("Could not notify this user. Please try again.", show_alert=True)
        return

    await mark_admin_message(callback, "✅ APPROVED")
    await callback.answer("Payment approved")


@dp.chat_join_request()
async def handle_join_request(request: ChatJoinRequest) -> None:
    """Approve channel requests only for users with active memberships."""
    if request.chat.id != CHANNEL_ID:
        logger.warning("Ignoring join request for unexpected chat_id=%s", request.chat.id)
        return

    user_id = request.from_user.id
    state = await membership_state(user_id)
    try:
        if state == "active":
            await bot.approve_chat_join_request(chat_id=CHANNEL_ID, user_id=user_id)
            logger.info("Approved join request for active member %s", user_id)
            return

        await bot.decline_chat_join_request(chat_id=CHANNEL_ID, user_id=user_id)
        if state == "expired":
            await bot.send_message(user_id, "Your membership has expired. Please renew.")
        else:
            await bot.send_message(user_id, "No active membership found.")
        logger.info("Declined join request for user %s with state=%s", user_id, state)
    except TelegramAPIError:
        logger.exception("Could not process join request for user %s", user_id)


@dp.callback_query(F.data.startswith("reject:"))
async def reject(callback: CallbackQuery) -> None:
    """Reject payment and notify the user."""
    user_id = await process_admin_callback(callback)
    if user_id is None:
        return

    user = await get_user(user_id)
    if user is None or user[5] != "Pending":
        await callback.answer("This payment has already been processed.", show_alert=True)
        return

    try:
        await bot.send_message(
            user_id,
            "<b>❌ Payment Rejected</b>\n\n"
            "We could not verify your payment. Please contact the administrator for assistance.",
        )
        await update_status(user_id, "Rejected")
    except TelegramAPIError:
        logger.exception("Rejection notification failed for user %s", user_id)
        await callback.answer("Could not reject this payment. Please try again.", show_alert=True)
        return

    await mark_admin_message(callback, "❌ REJECTED")
    await callback.answer("Payment rejected")


@dp.message(Command("stats"))
async def stats(message: Message) -> None:
    """Show aggregate membership statistics to the admin."""
    if not is_admin(message.from_user.id):
        return
    total, approved, rejected, pending = await asyncio.gather(
        total_users(), approved_users(), rejected_users(), pending_users()
    )
    await message.answer(
        "<b>📊 Bot Statistics</b>\n\n"
        f"👥 <b>Total users:</b> {total}\n"
        f"✅ <b>Approved:</b> {approved}\n"
        f"❌ <b>Rejected:</b> {rejected}\n"
        f"⏳ <b>Pending:</b> {len(pending)}"
    )


@dp.message(Command("pending"))
async def pending(message: Message) -> None:
    """Show pending-payment users to the admin."""
    if not is_admin(message.from_user.id):
        return
    users = await pending_users()
    if not users:
        await message.answer("✅ There are no pending payment verifications.")
        return

    lines = ["<b>⏳ Pending Payments</b>"]
    for user in users:
        lines.extend(
            [
                "",
                f"👤 <b>{escape(str(user[2]))}</b>",
                f"🆔 <code>{user[0]}</code>",
                f"📦 {escape(str(user[3]))} — ₹{user[4]}",
            ]
        )
    await message.answer("\n".join(lines))


def format_membership_date(value: str | None) -> str:
    """Format a stored membership timestamp for an administrator."""
    parsed = datetime_from_storage(value)
    return parsed.strftime("%d %b %Y %H:%M UTC") if parsed else "Not set"


def format_member(user: tuple[object, ...], date_index: int) -> str:
    """Create one concise admin-facing membership line."""
    return (
        f"<code>{user[0]}</code> — {escape(str(user[2]))}\n"
        f"{escape(str(user[3]))} — expires: {format_membership_date(user[date_index])}"
    )


@dp.message(Command("active"))
async def active(message: Message) -> None:
    """Show active memberships and their expiry dates to the administrator."""
    if not is_admin(message.from_user.id):
        return
    users = await active_users()
    if not users:
        await message.answer("There are no active memberships.")
        return
    lines = ["<b>Active Members</b>"]
    lines.extend(format_member(user, 8) for user in users)
    await message.answer("\n\n".join(lines))


@dp.message(Command("expired"))
async def expired(message: Message) -> None:
    """Show memberships removed after expiry to the administrator."""
    if not is_admin(message.from_user.id):
        return
    users = await expired_users()
    if not users:
        await message.answer("There are no expired memberships.")
        return
    lines = ["<b>Expired Members</b>"]
    lines.extend(format_member(user, 9) for user in users)
    await message.answer("\n\n".join(lines))


def command_arguments(message: Message) -> list[str]:
    """Extract whitespace-separated arguments from a bot command."""
    return (message.text or "").split()[1:]


@dp.message(Command("extend"))
async def extend(message: Message) -> None:
    """Extend a member's expiry date: /extend USER_ID DAYS."""
    if not is_admin(message.from_user.id):
        return
    arguments = command_arguments(message)
    if len(arguments) != 2:
        await message.answer("Usage: <code>/extend USER_ID DAYS</code>")
        return
    try:
        user_id, days = (int(value) for value in arguments)
        expiry_date = await extend_membership(user_id, days)
    except ValueError:
        await message.answer("USER_ID and DAYS must be positive whole numbers.")
        return
    if expiry_date is None:
        await message.answer("No membership record was found for that user.")
        return
    await message.answer(
        f"Membership extended for <code>{user_id}</code> until "
        f"<b>{expiry_date:%d %b %Y %H:%M UTC}</b>."
    )
    logger.info(
        "Admin %s extended user %s by %s days", message.from_user.id, user_id, days
    )


@dp.message(Command("remove"))
async def remove(message: Message) -> None:
    """Remove a member immediately: /remove USER_ID."""
    if not is_admin(message.from_user.id):
        return
    arguments = command_arguments(message)
    if len(arguments) != 1:
        await message.answer("Usage: <code>/remove USER_ID</code>")
        return
    try:
        user_id = int(arguments[0])
    except ValueError:
        await message.answer("USER_ID must be a whole number.")
        return
    if await get_user(user_id) is None:
        await message.answer("No membership record was found for that user.")
        return
    try:
        await bot.ban_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        await bot.unban_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        await mark_expired(user_id)
    except TelegramAPIError:
        logger.exception("Admin removal failed for user %s", user_id)
        await message.answer(
            "Could not remove the user. Check bot channel permissions."
        )
        return
    logger.info(
        "Admin %s removed user %s from channel %s",
        message.from_user.id,
        user_id,
        CHANNEL_ID,
    )
    await message.answer(f"Removed <code>{user_id}</code> from the premium channel.")


@dp.message()
async def other_messages(message: Message) -> None:
    """Reply helpfully to unsupported user messages."""
    if message.from_user.id == ADMIN_ID:
        return
    await message.answer(
        "Please use /start to choose a membership plan. After payment, send your "
        "payment screenshot as a photo in this chat."
    )


async def validate_startup() -> None:
    """Validate token, admin, channel access, and invite permissions before polling."""
    try:
        me = await bot.get_me()
    except TelegramUnauthorizedError as exc:
        logger.critical("Bot token validation failed: the BOT_TOKEN is invalid or revoked.")
        raise RuntimeError("Invalid BOT_TOKEN. Generate a new token with @BotFather.") from exc
    except TelegramAPIError as exc:
        logger.critical("Bot token validation failed due to a Telegram API error: %s", exc)
        raise RuntimeError("Could not validate BOT_TOKEN with Telegram.") from exc

    logger.info("Bot token validated: @%s (ID: %s)", me.username, me.id)
    logger.info("Runtime ADMIN_ID loaded from .env/environment: %s", ADMIN_ID)
    logger.info("Runtime CHANNEL_ID loaded from .env/environment: %s", CHANNEL_ID)

    try:
        admin_chat = await bot.get_chat(ADMIN_ID)
        logger.info("Admin ID validated: %s (%s)", admin_chat.id, admin_chat.type)
    except TelegramForbiddenError:
        logger.warning("Admin ID %s is valid, but the admin has not started the bot.", ADMIN_ID)
    except TelegramBadRequest as exc:
        logger.warning("Could not verify ADMIN_ID %s: %s", ADMIN_ID, exc)
    except TelegramAPIError:
        logger.exception("Unexpected error while validating ADMIN_ID %s", ADMIN_ID)

    try:
        channel = await bot.get_chat(CHANNEL_ID)
    except TelegramBadRequest as exc:
        logger.exception(
            "Channel access failed for CHANNEL_ID=%s. Complete Telegram exception: %r. "
            "The ID is invalid, the channel does not exist, or the bot has not been added "
            "to it. Obtain the correct ID by forwarding a channel post to @userinfobot "
            "(it normally starts with -100), then add this bot as a channel administrator.",
            CHANNEL_ID,
            exc,
        )
        raise RuntimeError("Cannot access configured channel. See the log for correction steps.") from exc
    except TelegramForbiddenError as exc:
        logger.critical(
            "Channel access was forbidden for CHANNEL_ID=%s: %s. Add the bot to the "
            "channel and promote it to administrator.",
            CHANNEL_ID,
            exc,
        )
        raise RuntimeError("Bot cannot access configured channel.") from exc
    except TelegramAPIError as exc:
        logger.critical("Channel validation failed for CHANNEL_ID=%s: %s", CHANNEL_ID, exc)
        raise RuntimeError("Could not validate configured channel.") from exc

    logger.info("Channel ID: %s", channel.id)
    logger.info("Channel title: %s", channel.title)
    logger.info("Chat type: %s", channel.type)
    if channel.type not in {ChatType.CHANNEL, ChatType.SUPERGROUP}:
        raise RuntimeError(
            f"CHANNEL_ID {CHANNEL_ID} resolves to {channel.type!r}, not a channel or supergroup."
        )

    try:
        member = await bot.get_chat_member(CHANNEL_ID, me.id)
    except TelegramAPIError as exc:
        logger.critical(
            "Could not read bot permissions in channel %s: %s. Promote the bot to "
            "administrator and grant it permission to invite users.",
            CHANNEL_ID,
            exc,
        )
        raise RuntimeError("Could not validate bot channel permissions.") from exc

    can_invite_users = getattr(member, "can_invite_users", False)
    can_restrict_members = getattr(member, "can_restrict_members", False)
    logger.info(
        "Bot permissions: status=%s, can_invite_users=%s, can_restrict_members=%s",
        member.status,
        can_invite_users,
        can_restrict_members,
    )
    if member.status not in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}:
        raise RuntimeError("Bot is not a channel administrator. Promote it before starting the bot.")
    if not can_invite_users:
        raise RuntimeError("Bot cannot invite users. Enable the 'Invite Users via Link' admin permission.")
    if not can_restrict_members:
        raise RuntimeError("Bot cannot remove expired members. Enable the 'Ban Users' admin permission.")


async def main() -> None:
    """Initialize storage and start polling."""
    await init_db()
    scheduler_task: asyncio.Task[None] | None = None
    try:
        await validate_startup()
        await expire_due_memberships(bot)
        scheduler_task = asyncio.create_task(run_expiry_scheduler(bot))
        logger.info("Bot started successfully; polling is now active.")
        await dp.start_polling(bot)
    finally:
        if scheduler_task is not None:
            scheduler_task.cancel()
            try:
                await scheduler_task
            except asyncio.CancelledError:
                pass
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
