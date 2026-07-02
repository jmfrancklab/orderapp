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

    if not os.path.exists(DB_PATH):
        print(f"Database not found at {DB_PATH}.\n"
              "Start the app once first so init_db() creates it, then re-run this script.",
              file=sys.stderr)
        sys.exit(1)

    db = sqlite3.connect(DB_PATH)
    cur = db.execute(
        "INSERT OR IGNORE INTO allowed_emails (email, added_by, added_at) VALUES (?, ?, ?)",
        (email, "setup-script", datetime.datetime.utcnow().isoformat()))
    db.commit()
    db.close()

    if cur.rowcount:
        print(f"Added: {email}")
    else:
        print(f"Already present: {email}")


if __name__ == "__main__":
    main()
