import asyncio

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)

from config import BOT_TOKEN, ADMIN_ID, UPI_ID, UPI_NAME

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Stores selected plan for each user (temporary, in memory)
user_plans = {}


# ---------- START ----------

@dp.message(CommandStart())
async def start(message: Message):

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="1 Month - ₹199", callback_data="plan_1")],
            [InlineKeyboardButton(text="3 Months - ₹499", callback_data="plan_3")],
            [InlineKeyboardButton(text="6 Months - ₹899", callback_data="plan_6")],
            [InlineKeyboardButton(text="12 Months - ₹1299", callback_data="plan_12")],
        ]
    )

    await message.answer(
        "👋 Welcome to VIP User Premium!\n\n"
        "Choose a subscription plan:",
        reply_markup=keyboard,
    )


# ---------- PLAN SELECT ----------

@dp.callback_query()
async def choose_plan(callback: CallbackQuery):

    plans = {
        "plan_1": "1 Month - ₹199",
        "plan_3": "3 Months - ₹499",
        "plan_6": "6 Months - ₹899",
        "plan_12": "12 Months - ₹1299",
    }

    plan = plans.get(callback.data)

    # Save selected plan
    user_plans[callback.from_user.id] = plan

    await callback.message.answer(
        f"""
✅ <b>{plan}</b>

💳 <b>Pay using UPI</b>

<b>UPI ID:</b>
<code>{UPI_ID}</code>

<b>UPI Name:</b>
{UPI_NAME}

━━━━━━━━━━━━━━━━━━━━

📸 After payment,
send the payment screenshot here.

Your subscription will be activated after verification.
""",
        parse_mode="HTML",
    )

    await callback.answer()


# ---------- RECEIVE SCREENSHOT ----------

@dp.message(F.photo)
async def payment_screenshot(message: Message):

    username = (
        f"@{message.from_user.username}"
        if message.from_user.username
        else "No Username"
    )

    plan = user_plans.get(message.from_user.id, "Not Selected")

    caption = f"""
📥 NEW PAYMENT

👤 Name:
{message.from_user.full_name}

🆔 User ID:
{message.from_user.id}

👤 Username:
{username}

📦 Plan:
{plan}
"""

    await bot.send_photo(
        chat_id=ADMIN_ID,
        photo=message.photo[-1].file_id,
        caption=caption,
    )

    await message.answer(
        "✅ Payment screenshot received.\n\n"
        "Please wait while the admin verifies your payment."
    )


# ---------- MAIN ----------

async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())