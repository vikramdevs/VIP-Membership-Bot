"""Telegram VIP membership bot."""

import asyncio
import csv
import io
import logging
import os
import sqlite3
from datetime import datetime
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
    delete_user,
    datetime_from_storage,
    datetime_to_storage,
    DB_NAME,
    expired_users,
    extend_membership,
    get_active_users,
    get_all_users,
    get_expired_users,
    get_user,
    init_db,
    IST,
    mark_expired,
    membership_state,
    pending_users,
    rejected_users,
    save_user,
    save_join_request_link,
    save_admin_membership,
    search_user,
    reset_membership,
    total_users,
    update_status,
    update_membership_field,
)
from scheduler import expire_due_memberships, run_expiry_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
admin_input_mode: dict[int, str] = {}
admin_add_state: dict[int, dict[str, object]] = {}
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
            [InlineKeyboardButton(text="7 Days - Rs 99", callback_data="plan_7")],
            [InlineKeyboardButton(text="1 Month - Rs 199", callback_data="plan_1")],
            [InlineKeyboardButton(text="3 Months - Rs 499", callback_data="plan_3")],
            [InlineKeyboardButton(text="6 Months - Rs 899", callback_data="plan_6")],
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
        "<b>VIP User Premium</b>\n\n"
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
            f"<b>{escape(plan_name)} Membership</b>\n\n"
            f"<b>Amount:</b> Rs {amount}\n\n"
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
        await message.answer("Please choose a subscription plan before sending a screenshot.")
        return

    if user[5] == "Approved":
        await message.answer("Your membership is already approved. Please check your messages.")
        return

    if user[5] == "Rejected":
        await message.answer("This payment was rejected. Please choose a plan and submit a new payment.")
        return

    username = f"@{message.from_user.username}" if message.from_user.username else "Not set"
    caption = (
        "<b>New Payment Verification Request</b>\n\n"
        f"<b>Name:</b> {escape(message.from_user.full_name)}\n"
        f"<b>User ID:</b> <code>{message.from_user.id}</code>\n"
        f"<b>Username:</b> {escape(username)}\n"
        f"<b>Plan:</b> {escape(str(user[3]))}\n"
        f"<b>Amount:</b> Rs {user[4]}"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Approve", callback_data=f"approve:{message.from_user.id}")],
            [InlineKeyboardButton(text="Reject", callback_data=f"reject:{message.from_user.id}")],
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
        await message.answer("We could not submit your screenshot. Please try again shortly.")
        return

    await message.answer("Screenshot received. Please wait for admin verification.")


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
                [InlineKeyboardButton(text="Join Premium Channel", url=invite.invite_link)]
            ]
        )
        await save_join_request_link(user_id, invite.invite_link)
        approval = await approve_membership(user_id)
        if approval is None:
            logger.error("User %s disappeared before approval was saved", user_id)
            await callback.answer("Membership record no longer exists.", show_alert=True)
            return
        await bot.send_message(
            user_id,
            "<b>Payment Approved</b>\n\n"
            "Welcome to VIP Premium. Use the button below to request access to the "
            "private channel. Your request will be approved automatically while your "
            "membership is active.\n\n"
            "Your membership expires on\n\n"
            f"<b>{format_ist_datetime(approval.expiry_date)}</b>.",
            reply_markup=join_keyboard,
        )
    except TelegramAPIError:
        logger.exception("Approval notification failed for user %s", user_id)
        await callback.answer("Could not notify this user. Please try again.", show_alert=True)
        return

    logger.info(
        "Approved User: %s\nPlan: %s\nGranted: %s\nCurrent Time: %s\nExpiry: %s",
        user_id,
        approval.plan,
        approval.days_granted,
        format_ist_datetime(approval.approved_at).replace("\n", " "),
        format_ist_datetime(approval.expiry_date).replace("\n", " "),
    )
    await mark_admin_message(callback, "APPROVED")
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
            "<b>Payment Rejected</b>\n\n"
            "We could not verify your payment. Please contact the administrator for assistance.",
        )
        await update_status(user_id, "Rejected")
    except TelegramAPIError:
        logger.exception("Rejection notification failed for user %s", user_id)
        await callback.answer("Could not reject this payment. Please try again.", show_alert=True)
        return

    await mark_admin_message(callback, "REJECTED")
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
        "<b>Bot Statistics</b>\n\n"
        f"<b>Total users:</b> {total}\n"
        f"<b>Approved:</b> {approved}\n"
        f"<b>Rejected:</b> {rejected}\n"
        f"<b>Pending:</b> {len(pending)}"
    )


@dp.message(Command("pending"))
async def pending(message: Message) -> None:
    """Show pending-payment users to the admin."""
    if not is_admin(message.from_user.id):
        return
    users = await pending_users()
    if not users:
        await message.answer("There are no pending payment verifications.")
        return

    lines = ["<b>Pending Payments</b>"]
    for user in users:
        lines.extend(
            [
                "",
                f"<b>{escape(str(user[2]))}</b>",
                f"ID: <code>{user[0]}</code>",
                f"{escape(str(user[3]))} - Rs {user[4]}",
            ]
        )
    await message.answer("\n".join(lines))


def format_ist_datetime(value: datetime) -> str:
    """Display a timezone-aware timestamp in Indian Standard Time."""
    return value.astimezone(IST).strftime("%d %b %Y\n%I:%M %p IST")


def format_membership_date(value: str | None) -> str:
    """Format a stored membership timestamp for an administrator."""
    parsed = datetime_from_storage(value)
    return format_ist_datetime(parsed) if parsed else "Not set"


def format_member(user: tuple[object, ...], date_index: int) -> str:
    """Create one concise admin-facing membership line."""
    return (
        f"<code>{user[0]}</code> - {escape(str(user[2]))}\n"
        f"{escape(str(user[3]))} - expires: {format_membership_date(user[date_index])}"
    )


def admin_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Users", callback_data="adm:users:0")],
        [InlineKeyboardButton(text="⏳ Pending", callback_data="adm:pending:0")],
        [InlineKeyboardButton(text="🟢 Active", callback_data="adm:active:0")],
        [InlineKeyboardButton(text="🔴 Expired", callback_data="adm:expired:0")],
        [InlineKeyboardButton(text="🔍 Search User", callback_data="adm:search")],
        [InlineKeyboardButton(text="➕ Add User", callback_data="adm:adduser")],
        [InlineKeyboardButton(text="➕ Extend Membership", callback_data="adm:extendinput")],
        [InlineKeyboardButton(text="🗑 Delete User", callback_data="adm:deleteinput")],
        [InlineKeyboardButton(text="📊 Statistics", callback_data="adm:stats")],
        [InlineKeyboardButton(text="📤 Export Users", callback_data="adm:export")],
        [InlineKeyboardButton(text="💾 Backup Database", callback_data="adm:backup")],
        [InlineKeyboardButton(text="📥 Restore Database", callback_data="adm:restore")],
        [InlineKeyboardButton(text="📣 Broadcast Message", callback_data="adm:broadcast")],
        [InlineKeyboardButton(text="⬅ Close", callback_data="adm:close")],
    ])


def user_details(user: tuple[object, ...]) -> str:
    username = f"@{user[1]}" if user[1] else "Not set"
    return (
        f"<b>{escape(str(user[2]))}</b>\n\n"
        f"<b>User ID:</b> <code>{user[0]}</code>\n"
        f"<b>Username:</b> {escape(username)}\n"
        f"<b>Plan:</b> {escape(str(user[3]))}\n"
        f"<b>Status:</b> {escape(str(user[5]))}\n"
        f"<b>Amount:</b> Rs {user[4]}\n"
        f"<b>Approved Date:</b> {format_membership_date(user[7])}\n"
        f"<b>Expiry Date:</b> {format_membership_date(user[8])}"
    )


def user_actions(user_id: int, *, pending: bool = False, expired: bool = False) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if pending:
        rows.append([
            InlineKeyboardButton(text="✅ Approve", callback_data=f"adm:approve:{user_id}"),
            InlineKeyboardButton(text="❌ Reject", callback_data=f"adm:reject:{user_id}"),
        ])
    # A profile remains manageable even if its membership has expired.
    rows.append([InlineKeyboardButton(text="➕ Extend", callback_data=f"adm:extend:{user_id}")])
    rows.append([InlineKeyboardButton(text="🔄 Reset Membership", callback_data=f"adm:resetask:{user_id}")])
    rows.append([InlineKeyboardButton(text="✏️ Edit Membership", callback_data=f"adm:edit:{user_id}")])
    rows.append([InlineKeyboardButton(text="🗑 Delete", callback_data=f"adm:confirmdel:{user_id}")])
    rows.append([InlineKeyboardButton(text="⬅ Admin Menu", callback_data="adm:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def edit_admin_panel(callback: CallbackQuery, text: str, keyboard: InlineKeyboardMarkup | None = None) -> None:
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def remove_channel_membership(user: tuple[object, ...]) -> None:
    """Best-effort Telegram cleanup used before deleting or resetting a record."""
    user_id = int(user[0])
    invite_link = user[10]
    if invite_link:
        try:
            await bot.revoke_chat_invite_link(chat_id=CHANNEL_ID, invite_link=str(invite_link))
        except TelegramAPIError:
            logger.warning("Could not revoke invite link for user %s", user_id, exc_info=True)
    try:
        await bot.decline_chat_join_request(chat_id=CHANNEL_ID, user_id=user_id)
    except TelegramAPIError:
        # Telegram returns an error when there is no outstanding request.
        logger.debug("No pending join request to decline for user %s", user_id)
    try:
        await bot.ban_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        await bot.unban_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
    except TelegramAPIError:
        logger.warning("Could not remove user %s from channel", user_id, exc_info=True)


async def show_admin_list(callback: CallbackQuery, category: str, page: int) -> None:
    loaders = {
        "users": ("👥 All Users", get_all_users, False, False),
        "pending": ("⏳ Pending Users", pending_users, True, False),
        "active": ("🟢 Active Users", get_active_users, False, False),
        "expired": ("🔴 Expired Users", get_expired_users, False, True),
    }
    title, loader, is_pending, is_expired = loaders[category]
    users = await loader()
    per_page = 10
    page = max(0, min(page, max(0, (len(users) - 1) // per_page)))
    selected = users[page * per_page:(page + 1) * per_page]
    if not selected:
        await edit_admin_panel(callback, f"<b>{title}</b>\n\nNo users found.", admin_menu_keyboard())
        return
    lines = [f"<b>{title}</b>\n<i>Page {page + 1} of {(len(users) - 1) // per_page + 1}</i>"]
    keys: list[list[InlineKeyboardButton]] = []
    for number, user in enumerate(selected, start=page * per_page + 1):
        lines.append(
            f"\n<b>{number}.</b> {escape(str(user[2]))}\n"
            f"ID: <code>{user[0]}</code>\nPlan: {escape(str(user[3]))}\nStatus: {escape(str(user[5]))}"
        )
        if is_pending:
            keys.append([InlineKeyboardButton(text="Approve", callback_data=f"adm:approve:{user[0]}"), InlineKeyboardButton(text="Reject", callback_data=f"adm:reject:{user[0]}"), InlineKeyboardButton(text="Delete", callback_data=f"adm:confirmdel:{user[0]}")])
        elif is_expired:
            keys.append([InlineKeyboardButton(text="Delete", callback_data=f"adm:confirmdel:{user[0]}")])
        else:
            keys.append([InlineKeyboardButton(text="Delete", callback_data=f"adm:confirmdel:{user[0]}"), InlineKeyboardButton(text="Extend", callback_data=f"adm:extend:{user[0]}")])
    navigation: list[InlineKeyboardButton] = []
    if page:
        navigation.append(InlineKeyboardButton(text="◀ Previous", callback_data=f"adm:{category}:{page - 1}"))
    if (page + 1) * per_page < len(users):
        navigation.append(InlineKeyboardButton(text="Next ▶", callback_data=f"adm:{category}:{page + 1}"))
    if navigation:
        keys.append(navigation)
    keys.append([InlineKeyboardButton(text="⬅ Admin Menu", callback_data="adm:menu")])
    await edit_admin_panel(callback, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=keys))


def admin_plan_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="7 Days", callback_data=f"{prefix}:7"), InlineKeyboardButton(text="30 Days", callback_data=f"{prefix}:30")],
        [InlineKeyboardButton(text="90 Days", callback_data=f"{prefix}:90"), InlineKeyboardButton(text="180 Days", callback_data=f"{prefix}:180")],
        [InlineKeyboardButton(text="365 Days", callback_data=f"{prefix}:365")],
    ])


def plan_name_for_days(days: int) -> str:
    return {7: "7 Days", 30: "1 Month", 90: "3 Months", 180: "6 Months", 365: "12 Months"}[days]


async def activate_admin_added_user(admin_id: int, user_id: int, *, replace: bool) -> tuple[tuple[object, ...], datetime]:
    """Persist an add-user selection and issue its new channel invite link."""
    state = admin_add_state[admin_id]
    days = int(state["days"])
    if replace:
        expiry = await save_admin_membership(
            user_id, state["username"], str(state["full_name"]), plan_name_for_days(days),
            int(state["amount"]), days,
        )
    else:
        expiry = await extend_membership(user_id, days)
        if expiry is None:
            raise RuntimeError("Membership record disappeared")
    invite = await bot.create_chat_invite_link(chat_id=CHANNEL_ID, creates_join_request=True)
    await save_join_request_link(user_id, invite.invite_link)
    user = await get_user(user_id)
    if user is None:
        raise RuntimeError("Membership record disappeared")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Join Premium Channel", url=invite.invite_link)]])
    await bot.send_message(
        user_id,
        "✅ <b>Your membership has been activated.</b>\n\n"
        f"<b>Plan:</b> {escape(str(user[3]))}\n"
        f"<b>Expiry:</b> {format_ist_datetime(expiry)}",
        reply_markup=keyboard,
    )
    return user, expiry


@dp.message(Command("admin"))
async def admin_panel(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("❌ Access denied.")
        return
    admin_input_mode.pop(message.from_user.id, None)
    await message.answer("<b>Admin User Management</b>", reply_markup=admin_menu_keyboard())


@dp.callback_query(F.data.startswith("adm:"))
async def admin_panel_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Access denied.", show_alert=True)
        return
    if callback.data is None:
        return
    parts = callback.data.split(":")
    action = parts[1]
    if action == "menu":
        await edit_admin_panel(callback, "<b>Admin User Management</b>", admin_menu_keyboard())
    elif action == "close":
        await edit_admin_panel(callback, "<b>Admin panel closed.</b>")
    elif action in {"users", "pending", "active", "expired"}:
        await show_admin_list(callback, action, int(parts[2]))
    elif action in {"search", "extendinput", "deleteinput"}:
        admin_input_mode[callback.from_user.id] = action
        prompt = "Send User ID"
        await edit_admin_panel(callback, f"<b>{prompt}</b>\n\nUse a numeric Telegram User ID.", admin_menu_keyboard())
    elif action == "adduser":
        admin_add_state.pop(callback.from_user.id, None)
        admin_input_mode[callback.from_user.id] = "adduser"
        await edit_admin_panel(callback, "<b>Send Telegram User ID</b>\n\nExample: <code>6043346963</code>", admin_menu_keyboard())
    elif action == "addplan":
        state = admin_add_state.get(callback.from_user.id)
        if state is None:
            await callback.answer("Start Add User again.", show_alert=True)
            return
        state["days"] = int(parts[2])
        amount_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="99", callback_data="adm:addamount:99"), InlineKeyboardButton(text="199", callback_data="adm:addamount:199"), InlineKeyboardButton(text="499", callback_data="adm:addamount:499")],
            [InlineKeyboardButton(text="899", callback_data="adm:addamount:899"), InlineKeyboardButton(text="1299", callback_data="adm:addamount:1299")],
            [InlineKeyboardButton(text="Custom amount", callback_data="adm:addcustom")],
        ])
        await edit_admin_panel(callback, "<b>Enter payment amount</b>\n\nChoose a suggestion or use a custom amount.", amount_keyboard)
    elif action == "addamount":
        state = admin_add_state.get(callback.from_user.id)
        if state is None:
            await callback.answer("Start Add User again.", show_alert=True)
            return
        state["amount"] = int(parts[2])
        user_id = int(state["user_id"])
        existing = await get_user(user_id)
        if existing is not None:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Replace Membership", callback_data="adm:addreplace")],
                [InlineKeyboardButton(text="➕ Extend Existing", callback_data="adm:addextend")],
                [InlineKeyboardButton(text="❌ Cancel", callback_data="adm:menu")],
            ])
            await edit_admin_panel(callback, "<b>User already exists.</b>\n\nChoose how to apply the selected membership.", keyboard)
        else:
            try:
                user, expiry = await activate_admin_added_user(callback.from_user.id, user_id, replace=True)
            except TelegramAPIError:
                logger.exception("Could not activate manually added user %s", user_id)
                await callback.answer("Membership could not be activated. Check bot access to the user and channel.", show_alert=True)
                return
            admin_add_state.pop(callback.from_user.id, None)
            await edit_admin_panel(callback, f"✅ <b>User Added Successfully</b>\n\n<b>ID:</b> <code>{user_id}</code>\n<b>Name:</b> {escape(str(user[2]))}\n<b>Plan:</b> {escape(str(user[3]))}\n<b>Expiry:</b> {format_ist_datetime(expiry)}", admin_menu_keyboard())
    elif action == "addcustom":
        if callback.from_user.id not in admin_add_state:
            await callback.answer("Start Add User again.", show_alert=True)
            return
        admin_input_mode[callback.from_user.id] = "addamount"
        await edit_admin_panel(callback, "<b>Enter payment amount</b>\n\nSend a positive whole-number amount.")
    elif action in {"addreplace", "addextend"}:
        state = admin_add_state.get(callback.from_user.id)
        if state is None:
            await callback.answer("Start Add User again.", show_alert=True)
            return
        user_id = int(state["user_id"])
        try:
            user, expiry = await activate_admin_added_user(callback.from_user.id, user_id, replace=action == "addreplace")
        except TelegramAPIError:
            logger.exception("Could not apply manual membership for user %s", user_id)
            await callback.answer("Membership could not be activated. Check bot access to the user and channel.", show_alert=True)
            return
        admin_add_state.pop(callback.from_user.id, None)
        logger.info("Admin manually %s membership: admin=%s user=%s", "replaced" if action == "addreplace" else "extended", callback.from_user.id, user_id)
        await edit_admin_panel(callback, f"✅ <b>User Added Successfully</b>\n\n<b>ID:</b> <code>{user_id}</code>\n<b>Name:</b> {escape(str(user[2]))}\n<b>Plan:</b> {escape(str(user[3]))}\n<b>Expiry:</b> {format_ist_datetime(expiry)}", admin_menu_keyboard())
    elif action == "export":
        users = await get_all_users()
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(["user_id", "username", "full_name", "plan", "amount", "status", "expiry_days", "approved_at", "expiry_date", "expired_at", "join_request_link"])
        writer.writerows(users)
        from aiogram.types import BufferedInputFile
        await bot.send_document(callback.from_user.id, BufferedInputFile(stream.getvalue().encode("utf-8"), filename="users.csv"))
        await edit_admin_panel(callback, "✅ Users exported successfully.", admin_menu_keyboard())
    elif action == "backup":
        await bot.send_document(callback.from_user.id, FSInputFile(DB_NAME, filename="database.db"))
        await edit_admin_panel(callback, "✅ Database backup sent.", admin_menu_keyboard())
    elif action == "restore":
        admin_input_mode[callback.from_user.id] = "restore"
        await edit_admin_panel(callback, "<b>Upload database.db</b>\n\nThe current database will be replaced after basic SQLite validation.")
    elif action == "broadcast":
        admin_input_mode[callback.from_user.id] = "broadcast"
        await edit_admin_panel(callback, "<b>Send the broadcast message</b>\n\nIt will be delivered to all approved users.")
    elif action == "stats":
        total, approved, rejected, pending, active, expired = await asyncio.gather(total_users(), approved_users(), rejected_users(), pending_users(), get_active_users(), get_expired_users())
        await edit_admin_panel(callback, f"<b>📊 Statistics</b>\n\n<b>Total users:</b> {total}\n<b>Approved:</b> {approved}\n<b>Rejected:</b> {rejected}\n<b>Pending:</b> {len(pending)}\n<b>Active:</b> {len(active)}\n<b>Expired:</b> {len(expired)}", admin_menu_keyboard())
    else:
        user_id = int(parts[2])
        user = await search_user(user_id)
        if user is None:
            await callback.answer("User not found.", show_alert=True)
            return
        if action == "confirmdel":
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Yes", callback_data=f"adm:delete:{user_id}"), InlineKeyboardButton(text="❌ Cancel", callback_data=f"adm:view:{user_id}")]])
            await edit_admin_panel(callback, "<b>⚠ Delete this user?</b>", keyboard)
        elif action == "delete":
            await remove_channel_membership(user)
            await delete_user(user_id)
            logger.info("Admin deleted user: admin=%s user=%s", callback.from_user.id, user_id)
            await edit_admin_panel(callback, "✅ User deleted successfully.", admin_menu_keyboard())
        elif action == "view":
            await edit_admin_panel(callback, user_details(user), user_actions(user_id, pending=user[5] == "Pending", expired=user[5] == "Expired"))
        elif action == "edit":
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Change plan", callback_data=f"adm:editplan:{user_id}")],
                [InlineKeyboardButton(text="Change expiry date", callback_data=f"adm:editexpiry:{user_id}")],
                [InlineKeyboardButton(text="Change amount", callback_data=f"adm:editamount:{user_id}")],
                [InlineKeyboardButton(text="⬅ Back", callback_data=f"adm:view:{user_id}")],
            ])
            await edit_admin_panel(callback, f"<b>Edit Membership</b>\n\n{user_details(user)}", keyboard)
        elif action == "editplan":
            await edit_admin_panel(callback, "<b>Select Membership Plan</b>", admin_plan_keyboard(f"adm:editplansave:{user_id}"))
        elif action == "editplansave":
            days = int(parts[3])
            await update_membership_field(user_id, "plan", plan_name_for_days(days))
            await update_membership_field(user_id, "expiry_days", days)
            updated = await get_user(user_id)
            await edit_admin_panel(callback, "✅ Plan updated.\n\n" + user_details(updated), user_actions(user_id, pending=updated[5] == "Pending", expired=updated[5] == "Expired"))
        elif action == "editexpiry":
            admin_input_mode[callback.from_user.id] = f"editexpiry:{user_id}"
            await edit_admin_panel(callback, "<b>Send expiry date and time</b>\n\nFormat: <code>DD-MM-YYYY HH:MM</code> (IST)")
        elif action == "editamount":
            admin_input_mode[callback.from_user.id] = f"editamount:{user_id}"
            await edit_admin_panel(callback, "<b>Enter payment amount</b>\n\nSend a positive whole-number amount.")
        elif action == "extend":
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="+7 Days", callback_data=f"adm:extenddays:{user_id}:7"), InlineKeyboardButton(text="+30 Days", callback_data=f"adm:extenddays:{user_id}:30")],
                [InlineKeyboardButton(text="+90 Days", callback_data=f"adm:extenddays:{user_id}:90"), InlineKeyboardButton(text="+180 Days", callback_data=f"adm:extenddays:{user_id}:180")],
                [InlineKeyboardButton(text="+365 Days", callback_data=f"adm:extenddays:{user_id}:365")],
                [InlineKeyboardButton(text="⬅ Back", callback_data=f"adm:view:{user_id}")],
            ])
            await edit_admin_panel(callback, f"<b>Extend membership for</b> <code>{user_id}</code>", keyboard)
        elif action == "extenddays":
            days = int(parts[3])
            expiry = await extend_membership(user_id, days)
            logger.info("Admin extended membership: admin=%s user=%s days=%s", callback.from_user.id, user_id, days)
            await edit_admin_panel(callback, f"✅ Membership extended by <b>{days} days</b>.\n\nNew expiry: <b>{format_ist_datetime(expiry)}</b>", user_actions(user_id))
        elif action == "resetask":
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Yes", callback_data=f"adm:reset:{user_id}"), InlineKeyboardButton(text="❌ Cancel", callback_data=f"adm:view:{user_id}")]])
            await edit_admin_panel(callback, "<b>⚠ Reset this membership?</b>", keyboard)
        elif action == "reset":
            await remove_channel_membership(user)
            await reset_membership(user_id)
            await edit_admin_panel(callback, "✅ Membership reset successfully.", admin_menu_keyboard())
        elif action == "reject":
            await update_status(user_id, "Rejected")
            try:
                await bot.send_message(user_id, "<b>Payment Rejected</b>\n\nWe could not verify your payment. Please contact the administrator for assistance.")
            except TelegramAPIError:
                logger.warning("Could not notify rejected user %s", user_id, exc_info=True)
            await edit_admin_panel(callback, "✅ User rejected.", admin_menu_keyboard())
        elif action == "approve":
            try:
                invite = await bot.create_chat_invite_link(chat_id=CHANNEL_ID, creates_join_request=True)
                await save_join_request_link(user_id, invite.invite_link)
                approval = await approve_membership(user_id)
                if approval is None:
                    await callback.answer("User record no longer exists.", show_alert=True)
                    return
                join_keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Join Premium Channel", url=invite.invite_link)]])
                await bot.send_message(user_id, "<b>Payment Approved</b>\n\nUse the button below to request access to the private channel.\n\nYour membership expires on\n\n" + f"<b>{format_ist_datetime(approval.expiry_date)}</b>.", reply_markup=join_keyboard)
            except TelegramAPIError:
                logger.exception("Panel approval failed for user %s", user_id)
                await callback.answer("Could not approve this user. Check channel permissions.", show_alert=True)
                return
            logger.info("Admin approved user: admin=%s user=%s", callback.from_user.id, user_id)
            await edit_admin_panel(callback, "✅ User approved successfully.", admin_menu_keyboard())
    await callback.answer()


@dp.message(F.text & ~F.text.startswith("/"))
async def admin_panel_user_id_input(message: Message) -> None:
    mode = admin_input_mode.get(message.from_user.id)
    if mode is None:
        if not is_admin(message.from_user.id):
            await message.answer(
                "Please use /start to choose a membership plan. After payment, send your "
                "payment screenshot as a photo in this chat."
            )
        return
    if not is_admin(message.from_user.id):
        await message.answer("❌ Access denied.")
        return
    if mode == "adduser":
        try:
            user_id = int(message.text.strip())
        except ValueError:
            await message.answer("Send a numeric Telegram User ID.")
            return
        try:
            chat = await bot.get_chat(user_id)
        except TelegramAPIError:
            await message.answer("Could not fetch this user with getChat. The user must have started the bot.")
            return
        full_name = getattr(chat, "full_name", None) or " ".join(
            part for part in (getattr(chat, "first_name", None), getattr(chat, "last_name", None)) if part
        ) or str(user_id)
        admin_add_state[message.from_user.id] = {
            "user_id": user_id, "username": getattr(chat, "username", None), "full_name": full_name,
        }
        admin_input_mode.pop(message.from_user.id, None)
        await message.answer("<b>Select Membership Plan</b>", reply_markup=admin_plan_keyboard("adm:addplan"))
        return
    if mode == "addamount":
        try:
            amount = int(message.text.strip())
            if amount <= 0:
                raise ValueError
        except ValueError:
            await message.answer("Send a positive whole-number amount.")
            return
        state = admin_add_state.get(message.from_user.id)
        if state is None:
            admin_input_mode.pop(message.from_user.id, None)
            await message.answer("Start Add User again from /admin.")
            return
        state["amount"] = amount
        admin_input_mode.pop(message.from_user.id, None)
        user_id = int(state["user_id"])
        if await get_user(user_id):
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Replace Membership", callback_data="adm:addreplace")],
                [InlineKeyboardButton(text="➕ Extend Existing", callback_data="adm:addextend")],
                [InlineKeyboardButton(text="❌ Cancel", callback_data="adm:menu")],
            ])
            await message.answer("<b>User already exists.</b>", reply_markup=keyboard)
        else:
            try:
                user, expiry = await activate_admin_added_user(message.from_user.id, user_id, replace=True)
            except TelegramAPIError:
                logger.exception("Could not activate manually added user %s", user_id)
                await message.answer("Membership could not be activated. Check bot access to the user and channel.")
                return
            admin_add_state.pop(message.from_user.id, None)
            await message.answer(f"✅ <b>User Added Successfully</b>\n\n<b>ID:</b> <code>{user_id}</code>\n<b>Name:</b> {escape(str(user[2]))}\n<b>Plan:</b> {escape(str(user[3]))}\n<b>Expiry:</b> {format_ist_datetime(expiry)}")
        return
    if mode == "broadcast":
        admin_input_mode.pop(message.from_user.id, None)
        recipients = [user[0] for user in await get_all_users() if user[5] == "Approved"]
        delivered = 0
        for user_id in recipients:
            try:
                await bot.send_message(int(user_id), message.text)
                delivered += 1
            except TelegramAPIError:
                logger.warning("Broadcast failed for user %s", user_id)
        await message.answer(f"✅ Broadcast sent to <b>{delivered}</b> approved users.")
        return
    if mode.startswith("editamount:"):
        try:
            amount = int(message.text.strip())
            if amount <= 0:
                raise ValueError
        except ValueError:
            await message.answer("Send a positive whole-number amount.")
            return
        user_id = int(mode.split(":", maxsplit=1)[1])
        await update_membership_field(user_id, "amount", amount)
        admin_input_mode.pop(message.from_user.id, None)
        await message.answer("✅ Amount updated.", reply_markup=user_actions(user_id))
        return
    if mode.startswith("editexpiry:"):
        try:
            expiry = datetime.strptime(message.text.strip(), "%d-%m-%Y %H:%M").replace(tzinfo=IST)
        except ValueError:
            await message.answer("Use format: <code>DD-MM-YYYY HH:MM</code> (IST).")
            return
        user_id = int(mode.split(":", maxsplit=1)[1])
        await update_membership_field(user_id, "expiry_date", datetime_to_storage(expiry))
        admin_input_mode.pop(message.from_user.id, None)
        await message.answer("✅ Expiry date updated.", reply_markup=user_actions(user_id))
        return
    if mode == "restore":
        await message.answer("Upload the <code>database.db</code> file as a document.")
        return
    try:
        user_id = int(message.text.strip())
    except ValueError:
        await message.answer("Send a numeric User ID.")
        return
    admin_input_mode.pop(message.from_user.id, None)
    user = await search_user(user_id)
    if user is None:
        await message.answer("User not found.")
        return
    if mode == "deleteinput":
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Yes", callback_data=f"adm:delete:{user_id}"), InlineKeyboardButton(text="❌ Cancel", callback_data="adm:menu")]])
        await message.answer("<b>⚠ Delete this user?</b>", reply_markup=keyboard)
    elif mode == "extendinput":
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ Choose duration", callback_data=f"adm:extend:{user_id}")]])
        await message.answer(user_details(user), reply_markup=keyboard)
    else:
        logger.info("Admin searched user: admin=%s user=%s", message.from_user.id, user_id)
        await message.answer(user_details(user), reply_markup=user_actions(user_id, pending=user[5] == "Pending", expired=user[5] == "Expired"))


@dp.message(F.document)
async def restore_database_document(message: Message) -> None:
    """Restore a validated SQLite database only after the admin chose Restore."""
    if not is_admin(message.from_user.id):
        await message.answer("❌ Access denied.")
        return
    if admin_input_mode.get(message.from_user.id) != "restore":
        return
    document = message.document
    if document is None or document.file_name != "database.db":
        await message.answer("Upload a file named <code>database.db</code>.")
        return
    restore_path = Path(f"{DB_NAME}.restore")
    try:
        await bot.download(document, destination=restore_path)
        with sqlite3.connect(restore_path) as restored:
            check = restored.execute("PRAGMA quick_check").fetchone()
            table = restored.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'users'").fetchone()
        if check != ("ok",) or table is None:
            raise ValueError("not a valid membership database")
        os.replace(restore_path, DB_NAME)
    except (TelegramAPIError, OSError, sqlite3.DatabaseError, ValueError):
        logger.exception("Database restore failed")
        if restore_path.exists():
            restore_path.unlink()
        await message.answer("Database restore failed. Upload a valid database.db backup.")
        return
    admin_input_mode.pop(message.from_user.id, None)
    await message.answer("✅ Database restored successfully.")


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
        f"<b>{format_ist_datetime(expiry_date)}</b>."
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
