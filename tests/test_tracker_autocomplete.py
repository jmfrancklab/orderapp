"""Integration coverage for tracker email suggestions."""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as app_module
from app import app as flask_app


@pytest.fixture
def tracker_client(tmp_path, monkeypatch):
    test_db = str(tmp_path / "tracker-autocomplete.db")
    monkeypatch.setattr(app_module, "DB_PATH", test_db)
    app_module.init_db()

    conn = sqlite3.connect(test_db)
    conn.executemany(
        "INSERT INTO allowed_emails (email, added_by, added_at) VALUES (?,?,?)",
        [
            ("zebra@lab.org", "test", "2026-01-01"),
            ("buyer@lab.org", "test", "2026-01-02"),
            ("Alpha@lab.org", "test", "2026-01-03"),
            ("a&b@lab.org", "test", "2026-01-04"),
        ],
    )
    conn.executemany(
        "INSERT INTO orders (user_email, status, submitted_at) VALUES (?,?,?)",
        [
            ("buyer@lab.org", "draft", None),
            ("buyer@lab.org", "submitted", "2026-01-01"),
        ],
    )
    conn.commit()
    conn.close()

    flask_app.config["TESTING"] = True
    with flask_app.test_client() as client:
        with client.session_transaction() as session:
            session["email"] = "buyer@lab.org"
        yield client


@pytest.mark.parametrize("path", ["/orders", "/submitted"])
def test_tracker_inputs_offer_sorted_known_email_choices(tracker_client, path):
    response = tracker_client.get(path)
    assert response.status_code == 200
    page = response.get_data(as_text=True)

    assert 'class="tracker-input" type="email" list="tracker-email-options"' in page
    assert page.count('<datalist id="tracker-email-options">') == 1

    choices = [
        'value="a&amp;b@lab.org"',
        'value="Alpha@lab.org"',
        'value="buyer@lab.org"',
        'value="zebra@lab.org"',
    ]
    positions = [page.index(choice) for choice in choices]
    assert positions == sorted(positions)
