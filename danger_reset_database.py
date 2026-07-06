#!/usr/bin/env python3
"""Wipe one table or the entire database.

Usage:
    python danger_reset_database.py           -- list all tables and row counts
    python danger_reset_database.py <table>   -- wipe just that table (keeps structure)
    python danger_reset_database.py ALL       -- delete DB file and recreate it empty
"""
import os
import sqlite3
import sys

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orders.db")

if not os.path.exists(DB_PATH):
    print("No database found — nothing to do.")
    sys.exit(0)

if len(sys.argv) < 2:
    conn = sqlite3.connect(DB_PATH)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()]
    print("Tables in orders.db:")
    for t in tables:
        count = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        print(f"  {t:<32} {count} rows")
    conn.close()
    print()
    print("Usage:")
    print("  python danger_reset_database.py <table>   # wipe one table")
    print("  python danger_reset_database.py ALL       # wipe and recreate entire DB")
    sys.exit(0)

target = sys.argv[1]

if target == "ALL":
    print(f"This will PERMANENTLY DELETE {DB_PATH} and ALL data in it.")
    answer = input("Type YES to confirm: ").strip()
    if answer != "YES":
        print("Aborted.")
        sys.exit(1)
    os.remove(DB_PATH)
    print("Deleted.")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import app  # noqa: E402 — triggers init_db() on import
    print("Fresh database created.")
    print("Run add_user.py to authorize the first user.")
else:
    conn = sqlite3.connect(DB_PATH)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()]
    if target not in tables:
        print(f"Unknown table '{target}'.")
        print(f"Available: {', '.join(sorted(tables))}")
        conn.close()
        sys.exit(1)
    count = conn.execute(f'SELECT COUNT(*) FROM "{target}"').fetchone()[0]
    conn.close()
    print(f"This will DELETE all {count} rows from '{target}' (structure is kept).")
    answer = input("Type YES to confirm: ").strip()
    if answer != "YES":
        print("Aborted.")
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(f'DELETE FROM "{target}"')
    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()
    conn.close()
    print(f"Wiped '{target}' — {count} row(s) deleted.")
