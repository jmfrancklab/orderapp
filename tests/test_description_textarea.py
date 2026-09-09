"""Description textarea sizing coverage."""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as app_module


def test_description_rows_estimates_wrapping_and_caps_at_two_lines():
    assert app_module.description_rows("", 18) == 1
    assert app_module.description_rows("short description", 18) == 1
    assert app_module.description_rows("a description that wraps", 18) == 2
    assert app_module.description_rows("x" * 100, 18) == 2
    assert app_module.description_rows("first\nsecond", 32) == 2


@pytest.fixture
def description_client(tmp_path, monkeypatch):
    test_db = str(tmp_path / "description-test.db")
    monkeypatch.setattr(app_module, "DB_PATH", test_db)
    app_module.init_db()

    conn = sqlite3.connect(test_db)
    conn.executemany(
        "INSERT INTO orders (user_email, description, status) VALUES (?, ?, ?)",
        [
            ("buyer@lab.org", "short draft", "draft"),
            (
                "buyer@lab.org",
                "a draft description long enough to wrap",
                "draft",
            ),
            ("buyer@lab.org", "short & <safe>", "submitted"),
            (
                "buyer@lab.org",
                "a submitted description that wraps",
                "submitted",
            ),
        ],
    )
    conn.commit()
    submitted_id = conn.execute(
        "SELECT id FROM orders WHERE status = 'submitted' ORDER BY id LIMIT 1"
    ).fetchone()[0]
    conn.close()

    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        with client.session_transaction() as session:
            session["email"] = "buyer@lab.org"
        yield client, test_db, submitted_id


def test_order_pages_render_server_sized_description_textareas(
        description_client):
    client, _, _ = description_client

    drafts = client.get("/orders").get_data(as_text=True)
    assert 'rows="1"' in drafts
    assert 'rows="2"' in drafts
    assert "short draft</textarea>" in drafts
    assert "a draft description long enough to wrap</textarea>" in drafts

    submitted = client.get("/submitted").get_data(as_text=True)
    assert 'rows="1">short &amp; &lt;safe&gt;</textarea>' in submitted
    assert 'rows="2">a submitted description that wraps</textarea>' in submitted


def test_description_textarea_value_with_newline_autosaves(description_client):
    client, db_path, submitted_id = description_client
    description = "first line\nsecond line"

    response = client.post(
        f"/api/orders/{submitted_id}", json={"description": description})

    assert response.status_code == 200
    conn = sqlite3.connect(db_path)
    stored = conn.execute(
        "SELECT description FROM orders WHERE id = ?", (submitted_id,)
    ).fetchone()[0]
    conn.close()
    assert stored == description
