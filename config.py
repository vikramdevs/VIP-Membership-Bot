"""Configuration loaded from environment variables."""

import os

from dotenv import load_dotenv

load_dotenv(override=False)


def _required_setting(name: str) -> str:
    """Return a required environment setting with a useful startup error."""
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required. Add it to the .env file.")
    return value


def _integer_setting(name: str) -> int:
    """Read a required integer environment setting."""
    value = _required_setting(name)
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a numeric Telegram ID; got {value!r}.") from exc


BOT_TOKEN = _required_setting("BOT_TOKEN")
ADMIN_ID = _integer_setting("ADMIN_ID")
CHANNEL_ID = _integer_setting("CHANNEL_ID")

UPI_ID = os.getenv("UPI_ID", "yadavvikram@fam")
UPI_NAME = os.getenv("UPI_NAME", "VIP User Premium")
