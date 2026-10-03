"""Submitted orders without trackers remain visible when filtering is cleared."""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import app as app_module


@pytest.mark.parametrize("authorized", [0, 1])
def test_other_users_untracked_order_is_visible_with_all_trackers(
    tmp_path, monkeypatch, authorized
):
    db_path = str(tmp_path / "visibility.db")
    monkeypatch.setattr(app_module, "DB_PATH", db_path)
    monkeypatch.setitem(app_module.app.config, "TESTING", True)
    app_module.init_db()
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO allowed_emails "
            "(email, added_by, added_at, expenditure_authorization) VALUES (?,?,?,?)",
            ("viewer@lab.org", "test", "2026-01-01", authorized),
        )
        order_id = db.execute(
            "INSERT INTO orders (user_email, description, status, submitted_at) "
            "VALUES (?,?,?,?)",
            ("creator@lab.org", "Untracked order", "submitted", "2026-01-01"),
        ).lastrowid
        assert db.execute("SELECT COUNT(*) FROM trackers").fetchone()[0] == 0

    marker = f'data-id="{order_id}"'.encode()
    with app_module.app.test_client() as client:
        with client.session_transaction() as session:
            session["email"] = "viewer@lab.org"
        for url in ("/submitted", "/submitted?tracker=viewer%40lab.org"):
            response = client.get(url)
            assert response.status_code == 200
            assert marker not in response.data
        response = client.get("/submitted?tracker=all")
        assert response.status_code == 200
        assert marker in response.data
        assert marker in client.get("/submitted?tracker=all").data

        # Clearing Trackers preserves other column filters.
        response = client.get("/submitted?tracker=all&filter_description=unrelated")
        assert response.status_code == 200
        assert marker not in response.data
