#!/usr/bin/env python3
"""Reset a local user's password so they can choose a new one at sign-in.

Usage:
    python3 reset_password.py user@example.com
"""
import os
import sqlite3
import sys


DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orders.db")


def main(args=None):
    args = sys.argv[1:] if args is None else args
    if args:
        email = args[0].strip().lower()
    else:
        email = input("Email address to reset: ").strip().lower()

    if "@" not in email or "." not in email.split("@")[-1]:
        print("Not a valid email address.", file=sys.stderr)
        return 1

    if not os.path.exists(DB_PATH):
        print(f"Database not found: {DB_PATH}", file=sys.stderr)
        return 1

    try:
        db = sqlite3.connect(DB_PATH)
        row = db.execute(
            "SELECT password_hash FROM allowed_emails WHERE email = ?", (email,)
        ).fetchone()
    except sqlite3.OperationalError as exc:
        print(f"Could not read local user passwords: {exc}", file=sys.stderr)
        return 1

    if row is None:
        db.close()
        print(f"No allowed user found for: {email}", file=sys.stderr)
        return 1

    if row[0] is None:
        db.close()
        print(f"Password is already reset for: {email}")
    else:
        db.execute(
            "UPDATE allowed_emails SET password_hash = NULL WHERE email = ?",
            (email,),
        )
        db.commit()
        db.close()
        print(f"Password reset for: {email}")

    print("The user can now sign in and choose a new password.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
