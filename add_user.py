#!/usr/bin/env python3
"""Add an email address to the allowed_emails table.

Run this from the orderapp directory to authorize the first (or any) user
before the app is running or before anyone can reach the Users tab.

Usage:
    python3 add_user.py your@email.com
"""
import datetime
import os
import sqlite3
import sys

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orders.db")


def main():
    if len(sys.argv) > 1:
        email = sys.argv[1].strip().lower()
    else:
        email = input("Email address to allow: ").strip().lower()

    if "@" not in email or "." not in email.split("@")[-1]:
        print("Not a valid email address.", file=sys.stderr)
        sys.exit(1)

    db = sqlite3.connect(DB_PATH)
    db.execute("""
        CREATE TABLE IF NOT EXISTS allowed_emails (
            id INTEGER PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            added_by TEXT NOT NULL DEFAULT 'system',
            added_at TEXT NOT NULL
        )
    """)
    cur = db.execute(
        "INSERT OR IGNORE INTO allowed_emails (email, added_by, added_at) VALUES (?, ?, ?)",
        (email, "setup-script", datetime.datetime.now(datetime.timezone.utc).isoformat()))
    db.commit()
    db.close()

    if cur.rowcount:
        print(f"Added: {email}")
    else:
        print(f"Already present: {email}")


if __name__ == "__main__":
    main()
