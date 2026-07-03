#!/usr/bin/env python3
"""Delete orders.db and recreate it from scratch via init_db().

This permanently destroys all orders, vendors, projects, history, and
allowed users.  Run only on a development or freshly deployed instance.
"""
import os
import sys

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orders.db")

if not os.path.exists(DB_PATH):
    print("No database found — nothing to do.")
    sys.exit(0)

print(f"This will PERMANENTLY DELETE {DB_PATH} and all data in it.")
answer = input("Type YES to confirm: ").strip()
if answer != "YES":
    print("Aborted.")
    sys.exit(1)

os.remove(DB_PATH)
print("Deleted.")

# Recreate via the app's own init_db so the schema is always in sync.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app  # noqa: E402 — triggers init_db() on import
print("Fresh database created.")
print("Run add_user.py to authorize the first user.")
