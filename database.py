import sqlite3

conn = sqlite3.connect("vip.db")
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY,
    name TEXT,
    username TEXT,
    plan TEXT,
    status TEXT,
    expiry TEXT
)
""")

conn.commit()


def add_user(user_id, name, username, plan):
    cursor.execute("""
    INSERT OR REPLACE INTO users
    VALUES(?,?,?,?,?,?)
    """, (
        user_id,
        name,
        username,
        plan,
        "Pending",
        ""
    ))
    conn.commit()


def approve_user(user_id, expiry):
    cursor.execute("""
    UPDATE users
    SET status=?, expiry=?
    WHERE user_id=?
    """, (
        "Approved",
        expiry,
        user_id
    ))
    conn.commit()


def reject_user(user_id):
    cursor.execute("""
    UPDATE users
    SET status=?
    WHERE user_id=?
    """, (
        "Rejected",
        user_id
    ))
    conn.commit()