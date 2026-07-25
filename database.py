"""SQLite persistence helpers for the membership bot."""

from datetime import datetime, timedelta, timezone
from typing import Literal, TypeAlias

import aiosqlite

DB_NAME = "database.db"
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


def utc_now() -> datetime:
    """Return the current timezone-aware UTC time."""
    return datetime.now(timezone.utc)


def datetime_to_storage(value: datetime) -> str:
    """Format a UTC datetime for lexicographically sortable SQLite storage."""
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def datetime_from_storage(value: str | None) -> datetime | None:
    """Parse a stored UTC datetime, accepting legacy SQLite values if present."""
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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


async def approve_membership(user_id: int) -> datetime | None:
    """Approve a plan and extend an existing unexpired membership if applicable."""
    now = utc_now()
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT expiry_days, expiry_date FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        current_expiry = datetime_from_storage(row[1])
        base_date = current_expiry if current_expiry and current_expiry > now else now
        expiry_date = base_date + timedelta(days=int(row[0]))
        await db.execute(
            """
            UPDATE users
            SET status = 'Approved', approved_at = ?, expiry_date = ?, expired_at = NULL
            WHERE user_id = ?
            """,
            (datetime_to_storage(now), datetime_to_storage(expiry_date), user_id),
        )
        await db.commit()
        return expiry_date


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
    now = utc_now()
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
    now = datetime_to_storage(utc_now())
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
    now = datetime_to_storage(utc_now())
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
            (datetime_to_storage(utc_now()), user_id),
        )
        await db.commit()


async def extend_membership(user_id: int, days: int) -> datetime | None:
    """Add days to a membership, starting from its current expiry or now."""
    if days <= 0:
        raise ValueError("days must be positive")
    now = utc_now()
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
