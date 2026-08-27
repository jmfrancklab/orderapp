"""Integration coverage for guarded deletion on reference-data tabs."""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as app_module
from app import app as flask_app


@pytest.fixture
def reference_client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "reference-delete.db")
    monkeypatch.setattr(app_module, "DB_PATH", db_path)
    app_module.init_db()

    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT INTO allowed_emails (email, added_by, added_at) VALUES (?,?,?)",
        [
            ("buyer@lab.org", "test", "2026-01-01"),
            ("tracker@lab.org", "test", "2026-01-01"),
            ("unused@lab.org", "test", "2026-01-01"),
        ],
    )
    conn.executemany(
        "INSERT INTO vendors (name) VALUES (?)",
        [("Used vendor",), ("Unused vendor",)],
    )
    conn.executemany(
        "INSERT INTO projects (name) VALUES (?)",
        [("Used project",), ("Unused project",)],
    )
    conn.executemany(
        """INSERT INTO invoices (nickname, created_by, created_at)
           VALUES (?,?,?)""",
        [
            ("Used invoice", "buyer@lab.org", "2026-01-01"),
            ("Unused invoice", "buyer@lab.org", "2026-01-01"),
        ],
    )
    used_vendor = conn.execute(
        "SELECT id FROM vendors WHERE name='Used vendor'"
    ).fetchone()[0]
    used_project = conn.execute(
        "SELECT id FROM projects WHERE name='Used project'"
    ).fetchone()[0]
    used_invoice = conn.execute(
        "SELECT id FROM invoices WHERE nickname='Used invoice'"
    ).fetchone()[0]
    cursor = conn.execute(
        """INSERT INTO orders
           (user_email, description, vendor_id, project_id, invoice_id,
            status, order_status, submitted_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            "buyer@lab.org", "Blocking order", used_vendor, used_project,
            used_invoice, "submitted", "ordered", "2026-01-02",
        ),
    )
    conn.execute(
        "INSERT INTO trackers (order_id, email) VALUES (?,?)",
        (cursor.lastrowid, "tracker@lab.org"),
    )
    conn.commit()

    ids = {}
    for entity, table, label_field in [
        ("vendors", "vendors", "name"),
        ("projects", "projects", "name"),
        ("invoices", "invoices", "nickname"),
    ]:
        ids[(entity, "used")] = conn.execute(
            f"SELECT id FROM {table} WHERE {label_field} LIKE 'Used %'"
        ).fetchone()[0]
        ids[(entity, "unused")] = conn.execute(
            f"SELECT id FROM {table} WHERE {label_field} LIKE 'Unused %'"
        ).fetchone()[0]
    for kind, email in [
        ("used", "buyer@lab.org"),
        ("tracker", "tracker@lab.org"),
        ("unused", "unused@lab.org"),
    ]:
        ids[("users", kind)] = conn.execute(
            "SELECT id FROM allowed_emails WHERE email=?", (email,)
        ).fetchone()[0]
    conn.close()

    flask_app.config["TESTING"] = True
    with flask_app.test_client() as client:
        with client.session_transaction() as session:
            session["email"] = "buyer@lab.org"
        yield client, db_path, ids


@pytest.mark.parametrize(
    "entity,kind,relationship",
    [
        ("vendors", "used", "vendor_id"),
        ("projects", "used", "project_id"),
        ("invoices", "used", "invoice_id"),
        ("users", "used", "Submitter"),
        ("users", "tracker", "Tracker"),
    ],
)
def test_references_are_reported_and_block_deletion(
    reference_client, entity, kind, relationship
):
    client, db_path, ids = reference_client
    record_id = ids[(entity, kind)]
    check = client.get(
        f"/api/reference-records/{entity}/{record_id}/references"
    )
    assert check.status_code == 200
    data = check.get_json()
    assert data["can_delete"] is False
    assert data["reference_count"] == 1
    assert data["references"][0]["description"] == "Blocking order"
    assert data["references"][0]["relationship"] == relationship

    deletion = client.delete(f"/api/reference-records/{entity}/{record_id}")
    assert deletion.status_code == 409
    assert deletion.get_json()["reference_count"] == 1

    config = app_module.REFERENCE_DELETE_CONFIG[entity]
    conn = sqlite3.connect(db_path)
    still_exists = conn.execute(
        f"SELECT 1 FROM {config['table']} WHERE id=?", (record_id,)
    ).fetchone()
    conn.close()
    assert still_exists is not None


@pytest.mark.parametrize("entity", ["vendors", "projects", "invoices", "users"])
def test_unreferenced_record_can_be_deleted(reference_client, entity):
    client, db_path, ids = reference_client
    record_id = ids[(entity, "unused")]
    check = client.get(
        f"/api/reference-records/{entity}/{record_id}/references"
    )
    assert check.status_code == 200
    assert check.get_json()["can_delete"] is True

    deletion = client.delete(f"/api/reference-records/{entity}/{record_id}")
    assert deletion.status_code == 200
    assert deletion.get_json()["ok"] is True

    config = app_module.REFERENCE_DELETE_CONFIG[entity]
    conn = sqlite3.connect(db_path)
    assert conn.execute(
        f"SELECT 1 FROM {config['table']} WHERE id=?", (record_id,)
    ).fetchone() is None
    history = conn.execute(
        "SELECT old_value, new_value FROM order_history "
        "WHERE table_name=? AND order_id=?",
        (config["table"], record_id),
    ).fetchone()
    conn.close()
    assert history is not None
    assert history[1] is None


def test_invoice_creator_can_see_orphaned_invoice_to_delete_it(reference_client):
    client, _, ids = reference_client
    page = client.get("/invoices")
    assert page.status_code == 200
    assert b"Unused invoice" in page.data
    assert (
        f'data-record-id="{ids[("invoices", "unused")]}"'.encode()
        in page.data
    )


def test_delete_rechecks_after_confirmation_data_becomes_stale(reference_client):
    client, db_path, ids = reference_client
    vendor_id = ids[("vendors", "unused")]
    check = client.get(
        f"/api/reference-records/vendors/{vendor_id}/references"
    )
    assert check.get_json()["can_delete"] is True

    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO orders
           (user_email, description, vendor_id, status, order_status)
           VALUES (?,?,?,?,?)""",
        ("buyer@lab.org", "Late reference", vendor_id, "draft", "not ready"),
    )
    conn.commit()
    conn.close()

    deletion = client.delete(f"/api/reference-records/vendors/{vendor_id}")
    assert deletion.status_code == 409
    assert deletion.get_json()["references"][0]["description"] == "Late reference"

    conn = sqlite3.connect(db_path)
    assert conn.execute(
        "SELECT 1 FROM vendors WHERE id=?", (vendor_id,)
    ).fetchone()
    conn.close()


def test_legacy_form_endpoints_cannot_bypass_reference_guard(reference_client):
    client, db_path, ids = reference_client
    assert client.post(
        f"/vendors/{ids[('vendors', 'used')]}/delete"
    ).status_code == 302
    assert client.post(
        f"/users/{ids[('users', 'used')]}/remove"
    ).status_code == 302

    conn = sqlite3.connect(db_path)
    assert conn.execute(
        "SELECT 1 FROM vendors WHERE id=?", (ids[("vendors", "used")],)
    ).fetchone()
    assert conn.execute(
        "SELECT 1 FROM allowed_emails WHERE id=?", (ids[("users", "used")],)
    ).fetchone()
    conn.close()


@pytest.mark.parametrize(
    "path,entity",
    [
        ("/vendors", "vendors"),
        ("/projects", "projects"),
        ("/invoices", "invoices"),
        ("/users", "users"),
    ],
)
def test_reference_tabs_render_guarded_trash_buttons(reference_client, path, entity):
    client, _, _ = reference_client
    page = client.get(path)
    assert page.status_code == 200
    assert b'class="mini vdel reference-delete"' in page.data
    assert f'data-entity="{entity}"'.encode() in page.data


def test_unknown_reference_type_and_missing_record_return_404(reference_client):
    client, _, _ = reference_client
    assert client.get(
        "/api/reference-records/not-a-table/1/references"
    ).status_code == 404
    assert client.delete("/api/reference-records/vendors/999999").status_code == 404
