"""SQLite persistence helpers for the membership bot."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal, TypeAlias
from zoneinfo import ZoneInfo

import aiosqlite

DB_NAME = "database.db"
IST: Final = ZoneInfo("Asia/Kolkata")
UTC: Final = ZoneInfo("UTC")
PLAN_DURATIONS: Final[dict[str, int]] = {
    "7 Days": 7,
    "1 Month": 30,
    "3 Months": 90,
    "6 Months": 180,
    "12 Months": 365,
}
UserRow: TypeAlias = tuple[
    int,
    str | None,
    str,
    str,
    int,
    str,
    int,
    str | None,
    str | None,
    str | None,
    str | None,
]
MembershipState: TypeAlias = Literal["active", "expired", "inactive", "missing"]


@dataclass(frozen=True)
class ApprovalResult:
    """The timestamps and plan duration recorded for an approved membership."""

    approved_at: datetime
    expiry_date: datetime
    days_granted: int
    plan: str


def ist_now() -> datetime:
    """Return the current timezone-aware Indian Standard Time."""
    return datetime.now(IST)


def datetime_to_storage(value: datetime) -> str:
    """Format an aware datetime as an IST SQLite timestamp."""
    if value.tzinfo is None:
        raise ValueError("Cannot store a naive datetime.")
    return value.astimezone(IST).isoformat(timespec="seconds")


def datetime_from_storage(value: str | None) -> datetime | None:
    """Parse a stored timestamp and return a timezone-aware IST datetime."""
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(IST)


def plan_duration(plan: str) -> int:
    """Return the fixed number of days assigned to a membership plan."""
    try:
        return PLAN_DURATIONS[plan]
    except KeyError as exc:
        raise ValueError(f"Unknown membership plan: {plan!r}") from exc


def calculate_expiry(plan: str, current_time: datetime) -> tuple[datetime, int]:
    """Calculate an exact plan expiry from a timezone-aware base time."""
    if current_time.tzinfo is None:
        raise ValueError("Cannot calculate expiry from a naive datetime.")
    days = plan_duration(plan)
    return current_time.astimezone(IST) + timedelta(days=days), days


async def init_db() -> None:
    """Create the users table and migrate required membership columns."""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT NOT NULL,
                plan TEXT NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL,
                expiry_days INTEGER NOT NULL
            )
            """
        )
        cursor = await db.execute("PRAGMA table_info(users)")
        columns = {row[1] for row in await cursor.fetchall()}
        migrations = {
            "approved_at": "ALTER TABLE users ADD COLUMN approved_at TEXT",
            "expiry_date": "ALTER TABLE users ADD COLUMN expiry_date TEXT",
            "expired_at": "ALTER TABLE users ADD COLUMN expired_at TEXT",
            "join_request_link": "ALTER TABLE users ADD COLUMN join_request_link TEXT",
        }
        for column, statement in migrations.items():
            if column not in columns:
                await db.execute(statement)
        cursor = await db.execute(
            "SELECT user_id, approved_at, expiry_date, expired_at FROM users"
        )
        for user_id, approved_at, expiry_date, expired_at in await cursor.fetchall():
            normalized = tuple(
                datetime_to_storage(parsed)
                if (parsed := datetime_from_storage(value)) is not None
                else None
                for value in (approved_at, expiry_date, expired_at)
            )
            if normalized != (approved_at, expiry_date, expired_at):
                await db.execute(
                    """
                    UPDATE users
                    SET approved_at = ?, expiry_date = ?, expired_at = ?
                    WHERE user_id = ?
                    """,
                    (*normalized, user_id),
                )
        await db.commit()


async def save_user(
    user_id: int,
    username: str | None,
    full_name: str,
    plan: str,
    amount: int,
    expiry_days: int,
) -> None:
    """Save a selected plan and put its payment verification into pending state."""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            INSERT INTO users (
                user_id, username, full_name, plan, amount, status, expiry_days
            ) VALUES (?, ?, ?, ?, ?, 'Pending', ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                full_name = excluded.full_name,
                plan = excluded.plan,
                amount = excluded.amount,
                status = 'Pending',
                expiry_days = excluded.expiry_days
            """,
            (user_id, username, full_name, plan, amount, expiry_days),
        )
        await db.commit()


async def approve_membership(user_id: int) -> ApprovalResult | None:
    """Approve a plan and extend an existing unexpired membership if applicable."""
    now = ist_now()
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT plan, expiry_date FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        plan, expiry_value = row
        current_expiry = datetime_from_storage(expiry_value)
        base_date = current_expiry if current_expiry and current_expiry > now else now
        expiry_date, days_granted = calculate_expiry(plan, base_date)
        await db.execute(
            """
            UPDATE users
            SET status = 'Approved', approved_at = ?, expiry_date = ?, expired_at = NULL,
                expiry_days = ?
            WHERE user_id = ?
            """,
            (
                datetime_to_storage(now),
                datetime_to_storage(expiry_date),
                days_granted,
                user_id,
            ),
        )
        await db.commit()
        return ApprovalResult(now, expiry_date, days_granted, plan)


async def update_status(user_id: int, status: str) -> None:
    """Update a user's payment status."""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET status = ? WHERE user_id = ?", (status, user_id))
        await db.commit()


async def get_user(user_id: int) -> UserRow | None:
    """Return one user by Telegram ID."""
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return await cursor.fetchone()


async def delete_user(user_id: int) -> bool:
    """Permanently remove one user's membership record."""
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        await db.commit()
        return cursor.rowcount > 0


async def get_all_users() -> list[UserRow]:
    """Return every user for the administrator panel."""
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT * FROM users ORDER BY user_id")
        return await cursor.fetchall()


async def get_active_users() -> list[UserRow]:
    """Return approved memberships that are active at the current IST time."""
    now = datetime_to_storage(ist_now())
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            """
            SELECT * FROM users
            WHERE status = 'Approved' AND expiry_date IS NOT NULL AND expiry_date > ?
            ORDER BY expiry_date
            """,
            (now,),
        )
        return await cursor.fetchall()


async def get_expired_users() -> list[UserRow]:
    """Return memberships whose expiry time has passed, whether or not scheduled yet."""
    now = datetime_to_storage(ist_now())
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            """
            SELECT * FROM users
            WHERE expiry_date IS NOT NULL AND expiry_date <= ?
            ORDER BY expiry_date DESC
            """,
            (now,),
        )
        return await cursor.fetchall()


async def search_user(user_id: int) -> UserRow | None:
    """Find a user by their Telegram user ID."""
    return await get_user(user_id)


async def reset_membership(user_id: int) -> bool:
    """Return a user to pending state without deleting their payment record."""
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            """
            UPDATE users
            SET status = 'Pending', approved_at = NULL, expiry_date = NULL,
                expired_at = NULL, join_request_link = NULL
            WHERE user_id = ?
            """,
            (user_id,),
        )
        await db.commit()
        return cursor.rowcount > 0


async def save_join_request_link(user_id: int, invite_link: str) -> None:
    """Store the reusable join-request invite link issued to a member."""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE users SET join_request_link = ? WHERE user_id = ?",
            (invite_link, user_id),
        )
        await db.commit()


async def membership_state(user_id: int) -> MembershipState:
    """Return whether a user can currently have a channel join request approved."""
    now = ist_now()
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT status, expiry_date FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
    if row is None:
        return "missing"
    status, expiry_value = row
    expiry_date = datetime_from_storage(expiry_value)
    if status == "Expired" or (expiry_date is not None and expiry_date <= now):
        return "expired"
    if expiry_date is not None and expiry_date > now:
        return "active"
    return "inactive"


async def pending_users() -> list[UserRow]:
    """Return all users awaiting payment verification."""
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT * FROM users WHERE status = 'Pending' ORDER BY user_id")
        return await cursor.fetchall()


async def active_users() -> list[UserRow]:
    """Return memberships that have not yet expired, including pending renewals."""
    now = datetime_to_storage(ist_now())
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            """
            SELECT * FROM users
            WHERE expiry_date IS NOT NULL AND expiry_date > ? AND status != 'Expired'
            ORDER BY expiry_date
            """,
            (now,),
        )
        return await cursor.fetchall()


async def expired_users() -> list[UserRow]:
    """Return memberships marked expired."""
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT * FROM users WHERE status = 'Expired' ORDER BY expired_at DESC"
        )
        return await cursor.fetchall()


async def memberships_due_for_expiry() -> list[UserRow]:
    """Return memberships whose expiry time has passed and are not yet removed."""
    now = datetime_to_storage(ist_now())
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            """
            SELECT * FROM users
            WHERE expiry_date IS NOT NULL AND expiry_date <= ? AND status != 'Expired'
            ORDER BY expiry_date
            """,
            (now,),
        )
        return await cursor.fetchall()


async def mark_expired(user_id: int) -> None:
    """Mark a user expired after they have been removed from the channel."""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE users SET status = 'Expired', expired_at = ? WHERE user_id = ?",
            (datetime_to_storage(ist_now()), user_id),
        )
        await db.commit()


async def extend_membership(user_id: int, days: int) -> datetime | None:
    """Add days to a membership, starting from its current expiry or now."""
    if days <= 0:
        raise ValueError("days must be positive")
    now = ist_now()
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT expiry_date FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        if row is None:
            return None
        current_expiry = datetime_from_storage(row[0])
        base_date = current_expiry if current_expiry and current_expiry > now else now
        expiry_date = base_date + timedelta(days=days)
        await db.execute(
            """
            UPDATE users
            SET status = 'Approved', expiry_date = ?, expired_at = NULL
            WHERE user_id = ?
            """,
            (datetime_to_storage(expiry_date), user_id),
        )
        await db.commit()
        return expiry_date


async def _count_users(status: str | None = None) -> int:
    async with aiosqlite.connect(DB_NAME) as db:
        if status is None:
            cursor = await db.execute("SELECT COUNT(*) FROM users")
        else:
            cursor = await db.execute("SELECT COUNT(*) FROM users WHERE status = ?", (status,))
        row = await cursor.fetchone()
        return int(row[0])


async def total_users() -> int:
    """Return the total number of users."""
    return await _count_users()


async def approved_users() -> int:
    """Return the number of approved users."""
    return await _count_users("Approved")


async def rejected_users() -> int:
    """Return the number of rejected users."""
    return await _count_users("Rejected")
