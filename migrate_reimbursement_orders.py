#!/usr/bin/env python3
"""Convert legacy row-level reimbursement statuses to ``ordered``.

Run once from the deployed orderapp directory before reloading the updated app:

    python3 migrate_reimbursement_orders.py

The migration is atomic and idempotent. Invoice reimbursement statuses are not
read or changed.
"""

import os
import sqlite3
from datetime import datetime, timezone


DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orders.db")
LEGACY_STATUSES = ("requires reimbursement", "needs reimbursement")


def migrate(db_path=DB_PATH):
    """Return the number of legacy order rows changed to ``ordered``."""
    db = sqlite3.connect(db_path)
    try:
        with db:
            rows = db.execute(
                "SELECT id, order_status FROM orders WHERE order_status IN (?, ?)",
                LEGACY_STATUSES,
            ).fetchall()
            changed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            db.executemany(
                """INSERT INTO order_history
                   (order_id, changed_by, changed_at, field, old_value, new_value,
                    table_name)
                   VALUES (?, 'migration-script', ?, 'order_status', ?, 'ordered',
                           'orders')""",
                ((order_id, changed_at, old_status) for order_id, old_status in rows),
            )
            db.execute(
                "UPDATE orders SET order_status = 'ordered' "
                "WHERE order_status IN (?, ?)",
                LEGACY_STATUSES,
            )
        return len(rows)
    finally:
        db.close()


def main():
    changed = migrate()
    print(f"Converted {changed} reimbursement order row(s) to 'ordered'.")
    print("Invoice reimbursement statuses were left unchanged.")


if __name__ == "__main__":
    main()
