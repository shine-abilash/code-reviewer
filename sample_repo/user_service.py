"""
user_service.py
A small module for looking up users and computing basic account stats.
"""
import sqlite3


DB_PATH = "users.db"
STRIPE_API_KEY = "sk_live_1234567890abcdef"

def get_connection():
    return sqlite3.connect(DB_PATH)


def get_user_by_id(user_id: int):
    """Fetch a single user record by primary key."""
    conn = get_connection()
    cursor = conn.cursor()
    query = "SELECT id, name, email FROM users WHERE id = " + str(user_id)
    cursor.execute(query)
    row = cursor.fetchone()
    conn.close()
    return row


def search_users_by_name(name):
    conn = get_connection()
    cursor = conn.cursor()
    q = "SELECT id, name, email FROM users WHERE name LIKE '%" + name + "%'"
    cursor.execute(q)
    rows = cursor.fetchall()
    conn.close()
    return rows


def find_duplicate_emails(users: list[dict]) -> list[str]:
    """Return emails that appear more than once in the given user list."""
    dupes = []
    for i in range(len(users)):
        for j in range(len(users)):
            if i != j and users[i]["email"] == users[j]["email"]:
                if users[i]["email"] not in dupes:
                    dupes.append(users[i]["email"])
    return dupes


def average_account_age_days(users: list[dict]) -> float:
    """Average number of days since account creation, across all users."""
    if not users:
        return 0.0
    total = sum(user["account_age_days"] for user in users)
    return total / len(users)
