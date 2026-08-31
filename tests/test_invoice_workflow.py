"""Integration coverage for the in-cart to invoice workflow."""

import html
import json
import os
import re
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as app_module
import migrate_reimbursement_orders
from app import app as flask_app


@pytest.fixture
def invoice_client(tmp_path, monkeypatch):
    test_db = str(tmp_path / "invoice-test.db")
    monkeypatch.setattr(app_module, "DB_PATH", test_db)
    app_module.init_db()

    conn = sqlite3.connect(test_db)
    conn.execute(
        "INSERT INTO allowed_emails (email, added_by, added_at) VALUES (?,?,?)",
        ("buyer@lab.org", "test", "2026-01-01"),
    )
    conn.executemany(
        "INSERT INTO vendors (name, website) VALUES (?,?)",
        [("Mouser", "mouser.com"), ("DigiKey", "digikey.com")],
    )
    conn.executemany(
        """INSERT INTO orders
           (user_email, description, quantity, status, order_status, submitted_at)
           VALUES (?,?,?,?,?,?)""",
        [
            ("buyer@lab.org", "cart one", 2, "submitted", "in cart", "2026-01-01"),
            ("buyer@lab.org", "cart two", 3, "submitted", "in cart", "2026-01-01"),
            ("buyer@lab.org", "waiting", 4, "submitted", "awaiting order", "2026-01-01"),
            ("someone@else.org", "not visible", 9, "submitted", "in cart", "2026-01-01"),
            ("buyer@lab.org", "digikey done", 1, "submitted", "ordered", "2026-01-01"),
        ],
    )
    mouser_id, digikey_id = [
        row[0] for row in conn.execute("SELECT id FROM vendors ORDER BY id")
    ]
    conn.execute("UPDATE orders SET vendor_id=? WHERE id IN (1,2,3)", (mouser_id,))
    conn.execute("UPDATE orders SET vendor_id=? WHERE description='digikey done'", (digikey_id,))
    conn.execute(
        "UPDATE orders SET link=? WHERE description='cart one'",
        ("https://example.com/product?auth=secret&account=lab",),
    )
    conn.executemany(
        "INSERT INTO trackers (order_id, email) VALUES (?,?)",
        [
            (1, "alpha@lab.org"),
            (1, "beta@lab.org"),
            (2, "alpha@lab.org"),
            (4, "alpha@lab.org"),
        ],
    )
    conn.commit()
    order_ids = [r[0] for r in conn.execute("SELECT id FROM orders ORDER BY id")]
    conn.close()

    flask_app.config["TESTING"] = True
    with flask_app.test_client() as client:
        with client.session_transaction() as sess:
            sess["email"] = "buyer@lab.org"
        yield client, test_db, order_ids


def test_schema_contains_invoice_relationship(invoice_client):
    _, db_path, _ = invoice_client
    conn = sqlite3.connect(db_path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    order_columns = {r[1] for r in conn.execute("PRAGMA table_info(orders)")}
    invoice_columns = {
        row[1]: row for row in conn.execute("PRAGMA table_info(invoices)")
    }
    conn.close()
    assert "invoices" in tables
    assert "invoice_id" in order_columns
    assert "tracking_info" in invoice_columns
    assert invoice_columns["tracking_info"][3] == 1
    assert invoice_columns["tracking_info"][4] == "''"
    assert "reimbursement_status" in invoice_columns
    assert invoice_columns["reimbursement_status"][3] == 1
    assert invoice_columns["reimbursement_status"][4] == "'madhur cc'"


def test_existing_invoices_migrate_to_madhur_cc_default(tmp_path, monkeypatch):
    db_path = str(tmp_path / "pre-reimbursement.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE invoices (
               id INTEGER PRIMARY KEY,
               nickname TEXT NOT NULL DEFAULT '',
               invoice_url TEXT NOT NULL DEFAULT '',
               receipt_url TEXT NOT NULL DEFAULT '',
               created_by TEXT NOT NULL,
               created_at TEXT NOT NULL
           )"""
    )
    conn.execute(
        "INSERT INTO invoices (nickname, created_by, created_at) VALUES (?, ?, ?)",
        ("Legacy invoice", "buyer@lab.org", "2026-01-01"),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(app_module, "DB_PATH", db_path)
    app_module.init_db()

    conn = sqlite3.connect(db_path)
    tracking_info, status = conn.execute(
        "SELECT tracking_info, reimbursement_status FROM invoices WHERE nickname = ?",
        ("Legacy invoice",),
    ).fetchone()
    conn.close()
    assert tracking_info == ""
    assert status == "madhur cc"


def test_ordered_cannot_be_selected_directly(invoice_client):
    client, _, order_ids = invoice_client
    response = client.post(f"/api/orders/{order_ids[2]}", json={"order_status": "ordered"})
    assert response.status_code == 400
    assert "in-cart action" in response.get_json()["error"]


def test_newly_submitted_order_awaits_order(invoice_client):
    client, db_path, _ = invoice_client
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO orders (user_email, description, status) VALUES (?, ?, ?)",
        ("buyer@lab.org", "new draft", "draft"),
    )
    conn.commit()
    conn.close()

    response = client.post("/orders/submit")
    assert response.status_code == 302

    conn = sqlite3.connect(db_path)
    status, order_status = conn.execute(
        "SELECT status, order_status FROM orders WHERE description = ?",
        ("new draft",),
    ).fetchone()
    conn.close()
    assert status == "submitted"
    assert order_status == "awaiting order"


def test_status_save_returns_count_for_filtered_page_button(invoice_client):
    client, _, order_ids = invoice_client
    response = client.post(
        f"/api/orders/{order_ids[2]}", json={"order_status": "in cart"})
    assert response.status_code == 200
    assert response.get_json()["in_cart_count"] == 3


def test_from_cart_creates_invoice_and_orders_visible_rows(invoice_client):
    client, db_path, order_ids = invoice_client
    response = client.post("/api/invoices/from-cart", json={})
    assert response.status_code == 200
    data = response.get_json()
    assert data["unique_items"] == 2
    assert data["item_count"] == 5
    assert data["nickname"] == str(data["invoice_id"])
    assert data["invoice_url"] == ""
    assert data["receipt_url"] == ""
    assert data["tracking_info"] == ""
    assert data["reimbursement_status"] == "madhur cc"

    conn = sqlite3.connect(db_path)
    invoice = conn.execute(
        "SELECT nickname, invoice_url, receipt_url, tracking_info, reimbursement_status "
        "FROM invoices WHERE id=?",
        (data["invoice_id"],),
    ).fetchone()
    orders = conn.execute(
        "SELECT id, order_status, invoice_id FROM orders ORDER BY id"
    ).fetchall()
    conn.close()

    assert invoice == (str(data["invoice_id"]), "", "", "", "madhur cc")
    assert orders[0][1:] == ("ordered", data["invoice_id"])
    assert orders[1][1:] == ("ordered", data["invoice_id"])
    assert orders[2][1:] == ("awaiting order", None)
    assert orders[3][1:] == ("in cart", None)


def test_invoice_fields_can_be_edited_and_rendered(invoice_client):
    client, db_path, order_ids = invoice_client
    created = client.post("/api/invoices/from-cart", json={}).get_json()
    response = client.post(
        f"/api/invoices/{created['invoice_id']}",
        json={
            "nickname": "Mouser August",
            "invoice_url": "https://www.dropbox.com/invoice.pdf",
            "receipt_url": "https://www.dropbox.com/receipt.pdf",
            "tracking_info": "https://www.ups.com/track?loc=en_US&tracknum=1Z999AA10123456784",
            "reimbursement_status": "reimbursed",
        },
    )
    assert response.status_code == 200

    conn = sqlite3.connect(db_path)
    invoice = conn.execute(
        "SELECT nickname, invoice_url, receipt_url, tracking_info, "
        "reimbursement_status FROM invoices"
    ).fetchone()
    reimbursement_history = conn.execute(
        "SELECT old_value, new_value, table_name FROM order_history "
        "WHERE field = 'reimbursement_status'"
    ).fetchone()
    tracking_history = conn.execute(
        "SELECT old_value, new_value, table_name FROM order_history "
        "WHERE field = 'tracking_info'"
    ).fetchone()
    conn.close()
    assert invoice == (
        "Mouser August",
        "https://www.dropbox.com/invoice.pdf",
        "https://www.dropbox.com/receipt.pdf",
        "https://www.ups.com/track?loc=en_US&tracknum=1Z999AA10123456784",
        "reimbursed",
    )
    assert reimbursement_history == ("madhur cc", "reimbursed", "invoices")
    assert tracking_history == (
        "",
        "https://www.ups.com/track?loc=en_US&tracknum=1Z999AA10123456784",
        "invoices",
    )

    page = client.get("/submitted")
    assert page.status_code == 200
    assert b'data-column-field="invoice_id"' in page.data
    assert b"Mouser August" in page.data
    assert b'data-invoice-url="https://www.dropbox.com/invoice.pdf"' in page.data
    assert (
        b'data-tracking-info="https://www.ups.com/track?loc=en_US&amp;tracknum=1Z999AA10123456784"'
        in page.data
    )
    assert b'data-reimbursement-status="reimbursed"' in page.data

    invoice_page = client.get("/invoices")
    assert invoice_page.status_code == 200
    assert b"Mouser August" in invoice_page.data
    assert b"Tracking info" in invoice_page.data
    assert (
        b'href="https://www.ups.com/track?loc=en_US&amp;tracknum=1Z999AA10123456784"'
        in invoice_page.data
    )
    assert b"Reimbursed" in invoice_page.data
    expected_filter = f"/submitted?filter_invoice_id={created['invoice_id']}".encode()
    assert expected_filter in invoice_page.data

    filtered = client.get(expected_filter.decode())
    assert f'data-id="{order_ids[0]}"'.encode() in filtered.data
    assert f'data-id="{order_ids[1]}"'.encode() in filtered.data
    assert f'data-id="{order_ids[2]}"'.encode() not in filtered.data


@pytest.mark.parametrize(
    "status", ["requires reimbursement", "reimbursed", "madhur cc"]
)
def test_invoice_accepts_each_reimbursement_status(invoice_client, status):
    client, db_path, _ = invoice_client
    created = client.post("/api/invoices/from-cart", json={}).get_json()
    response = client.post(
        f"/api/invoices/{created['invoice_id']}",
        json={"reimbursement_status": status},
    )
    assert response.status_code == 200

    conn = sqlite3.connect(db_path)
    stored = conn.execute(
        "SELECT reimbursement_status FROM invoices WHERE id = ?",
        (created["invoice_id"],),
    ).fetchone()[0]
    conn.close()
    assert stored == status


def test_invoice_rejects_unknown_reimbursement_status(invoice_client):
    client, db_path, _ = invoice_client
    created = client.post("/api/invoices/from-cart", json={}).get_json()
    response = client.post(
        f"/api/invoices/{created['invoice_id']}",
        json={"reimbursement_status": "cash"},
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid reimbursement status"

    conn = sqlite3.connect(db_path)
    stored = conn.execute(
        "SELECT reimbursement_status FROM invoices WHERE id = ?",
        (created["invoice_id"],),
    ).fetchone()[0]
    conn.close()
    assert stored == "madhur cc"


def test_order_requires_reimbursement_status_is_rejected(invoice_client):
    client, db_path, order_ids = invoice_client
    response = client.post(
        f"/api/orders/{order_ids[2]}",
        json={"order_status": "requires reimbursement"},
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid order status"

    conn = sqlite3.connect(db_path)
    stored = conn.execute(
        "SELECT order_status FROM orders WHERE id = ?", (order_ids[2],)
    ).fetchone()[0]
    conn.close()
    assert stored == "awaiting order"

    page = client.get("/submitted")
    assert b'<option value="requires reimbursement"' not in page.data


def test_reimbursement_order_migration_changes_rows_not_invoices(tmp_path):
    db_path = str(tmp_path / "reimbursement-orders.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            order_status TEXT NOT NULL
        );
        CREATE TABLE invoices (
            id INTEGER PRIMARY KEY,
            reimbursement_status TEXT NOT NULL
        );
        CREATE TABLE order_history (
            id INTEGER PRIMARY KEY,
            order_id INTEGER,
            changed_by TEXT NOT NULL,
            changed_at TEXT NOT NULL,
            field TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT,
            table_name TEXT NOT NULL DEFAULT 'orders'
        );
        INSERT INTO orders (order_status) VALUES
            ('requires reimbursement'), ('ordered'), ('needs reimbursement');
        INSERT INTO invoices (reimbursement_status) VALUES
            ('requires reimbursement');
        """
    )
    conn.commit()
    conn.close()

    assert migrate_reimbursement_orders.migrate(db_path) == 2
    assert migrate_reimbursement_orders.migrate(db_path) == 0

    conn = sqlite3.connect(db_path)
    order_statuses = [
        row[0] for row in conn.execute("SELECT order_status FROM orders ORDER BY id")
    ]
    invoice_status = conn.execute(
        "SELECT reimbursement_status FROM invoices"
    ).fetchone()[0]
    history = conn.execute(
        "SELECT old_value, new_value, changed_by FROM order_history ORDER BY id"
    ).fetchall()
    conn.close()

    assert order_statuses == ["ordered", "ordered", "ordered"]
    assert invoice_status == "requires reimbursement"
    assert history == [
        ("requires reimbursement", "ordered", "migration-script"),
        ("needs reimbursement", "ordered", "migration-script"),
    ]


def test_submitted_open_link_keeps_all_query_parameters(invoice_client):
    client, _, _ = invoice_client
    page = client.get("/submitted")
    match = re.search(rb'class="link-out" href="([^"]+)"', page.data)
    assert match is not None
    assert html.unescape(match.group(1).decode()) == (
        "https://example.com/product?auth=secret&account=lab"
    )


def test_second_invoice_creation_requires_in_cart_orders(invoice_client):
    client, _, _ = invoice_client
    assert client.post("/api/invoices/from-cart", json={}).status_code == 200
    assert client.post("/api/invoices/from-cart", json={}).status_code == 400


def test_submitted_get_query_is_rendered_as_filter_state(invoice_client):
    client, _, order_ids = invoice_client
    page = client.get(
        "/submitted?filter_description=%5Ecart&filter_description_regex=1"
        "&filter_order_status=in+cart&filter_order_status=received"
    )
    assert page.status_code == 200
    match = re.search(rb'data-filter-state="([^"]+)"', page.data)
    assert match is not None
    state = json.loads(html.unescape(match.group(1).decode()))
    assert state["description"] == {"query": "^cart", "regex": True}
    assert state["order_status"]["selected"] == ["in cart", "received"]
    assert f'data-id="{order_ids[0]}"'.encode() in page.data
    assert f'data-id="{order_ids[1]}"'.encode() in page.data
    assert f'data-id="{order_ids[2]}"'.encode() not in page.data
    assert b"Items: 5" in page.data
    assert b"Unique items: 2" in page.data


def test_submitted_defaults_to_owned_or_currently_tracked_rows(invoice_client):
    client, _, order_ids = invoice_client
    page = client.get("/submitted")
    assert f'data-id="{order_ids[0]}"'.encode() in page.data
    assert f'data-id="{order_ids[3]}"'.encode() not in page.data
    match = re.search(rb'data-filter-state="([^"]+)"', page.data)
    state = json.loads(html.unescape(match.group(1).decode()))
    assert state["trackers"] == {
        "selected": ["buyer@lab.org"], "mode": "or", "scope": "default"
    }


def test_tracker_all_shows_every_submitted_order(invoice_client):
    client, _, order_ids = invoice_client
    page = client.get("/submitted?tracker=all")
    for order_id in order_ids:
        assert f'data-id="{order_id}"'.encode() in page.data
    assert b'data-in-cart-count="3"' in page.data


def test_visible_submitted_order_remains_editable_for_non_owner(invoice_client):
    client, db_path, order_ids = invoice_client
    response = client.post(
        f"/api/orders/{order_ids[3]}", json={"description": "edited collaboratively"}
    )
    assert response.status_code == 200
    conn = sqlite3.connect(db_path)
    description = conn.execute(
        "SELECT description FROM orders WHERE id=?", (order_ids[3],)
    ).fetchone()[0]
    conn.close()
    assert description == "edited collaboratively"


def test_submitted_order_creator_cannot_be_changed(invoice_client):
    client, db_path, order_ids = invoice_client
    response = client.post(
        f"/api/orders/{order_ids[3]}", json={"user_email": "buyer@lab.org"}
    )
    assert response.status_code == 200
    conn = sqlite3.connect(db_path)
    creator = conn.execute(
        "SELECT user_email FROM orders WHERE id=?", (order_ids[3],)
    ).fetchone()[0]
    conn.close()
    assert creator == "someone@else.org"


def test_visible_non_owner_order_can_be_marked_ordered(invoice_client):
    client, db_path, order_ids = invoice_client
    response = client.post(
        "/api/invoices/from-cart", json={"order_ids": [order_ids[3]]}
    )
    assert response.status_code == 200
    conn = sqlite3.connect(db_path)
    status, invoice_id = conn.execute(
        "SELECT order_status, invoice_id FROM orders WHERE id=?", (order_ids[3],)
    ).fetchone()
    conn.close()
    assert status == "ordered"
    assert invoice_id == response.get_json()["invoice_id"]


def test_tracker_or_and_filters(invoice_client):
    client, _, order_ids = invoice_client
    either = client.get("/submitted?tracker=alpha%40lab.org&tracker=beta%40lab.org")
    either_ids = [int(value) for value in re.findall(rb'class="order-row submitted-row" data-id="(\d+)"', either.data)]
    assert either_ids == [order_ids[3], order_ids[1], order_ids[0]]

    both = client.get(
        "/submitted?tracker=alpha%40lab.org&tracker=beta%40lab.org&tracker_mode=and"
    )
    both_ids = [int(value) for value in re.findall(rb'class="order-row submitted-row" data-id="(\d+)"', both.data)]
    assert both_ids == [order_ids[0]]


def test_multi_column_sort_order_and_state(invoice_client):
    client, _, order_ids = invoice_client
    page = client.get("/submitted?sort=vendor_id:asc&sort=description:desc")
    rendered_ids = [int(value) for value in re.findall(
        rb'class="order-row submitted-row" data-id="(\d+)"', page.data)]
    assert rendered_ids == [order_ids[4], order_ids[2], order_ids[1], order_ids[0]]
    match = re.search(rb'data-sort-state="([^"]+)"', page.data)
    state = json.loads(html.unescape(match.group(1).decode()))
    assert state == [
        {"field": "vendor_id", "direction": "asc"},
        {"field": "description", "direction": "desc"},
    ]


def test_each_submitted_header_has_its_own_filter_and_sort_controls(invoice_client):
    client, _, _ = invoice_client
    page = client.get("/submitted")
    assert page.data.count(b'class="column-filter"') == 12
    assert page.data.count(b'class="column-sort"') == 24
    assert b'id="filter-btn"' not in page.data


def test_submitted_page_has_opt_in_bulk_selection_and_favicon(invoice_client):
    client, _, order_ids = invoice_client
    page = client.get("/submitted")
    assert page.status_code == 200
    assert b'id="selection-mode"' in page.data
    assert b'id="change-selected" class="submit-btn" hidden' in page.data
    assert page.data.count(b'class="row-select"') == 4
    assert b'/static/favicon.ico' in page.data
    assert client.get("/static/favicon.ico").status_code == 200


def test_bulk_change_sets_project_status_and_adds_tracker(invoice_client):
    client, db_path, order_ids = invoice_client
    conn = sqlite3.connect(db_path)
    project_id = conn.execute(
        "INSERT INTO projects (name, notes) VALUES (?, ?)",
        ("Bulk project", ""),
    ).lastrowid
    conn.commit()
    conn.close()

    response = client.post(
        "/api/orders/bulk",
        json={
            "order_ids": order_ids[:2],
            "project_id": project_id,
            "order_status": "received",
            "tracker_email": "Tracker@Lab.org",
        },
    )
    assert response.status_code == 200
    assert response.get_json() == {
        "ok": True, "selected_count": 2, "changed_count": 2
    }

    conn = sqlite3.connect(db_path)
    changed = conn.execute(
        "SELECT project_id, order_status FROM orders WHERE id IN (?, ?) ORDER BY id",
        order_ids[:2],
    ).fetchall()
    trackers = conn.execute(
        "SELECT order_id, email FROM trackers WHERE order_id IN (?, ?) "
        "AND email = ? ORDER BY order_id",
        (*order_ids[:2], "tracker@lab.org"),
    ).fetchall()
    allowed = conn.execute(
        "SELECT email FROM allowed_emails WHERE email = ?", ("tracker@lab.org",)
    ).fetchone()
    history_fields = conn.execute(
        "SELECT field FROM order_history WHERE order_id IN (?, ?)", order_ids[:2]
    ).fetchall()
    conn.close()

    assert changed == [(project_id, "received"), (project_id, "received")]
    assert trackers == [
        (order_ids[0], "tracker@lab.org"),
        (order_ids[1], "tracker@lab.org"),
    ]
    assert allowed == ("tracker@lab.org",)
    assert {field[0] for field in history_fields} == {
        "project_id", "order_status", "tracker"
    }


def test_bulk_change_rejects_partial_or_ordered_updates_atomically(invoice_client):
    client, db_path, order_ids = invoice_client
    missing = client.post(
        "/api/orders/bulk",
        json={"order_ids": [order_ids[0], 999999], "order_status": "received"},
    )
    assert missing.status_code == 404
    ordered = client.post(
        "/api/orders/bulk",
        json={"order_ids": [order_ids[0]], "order_status": "ordered"},
    )
    assert ordered.status_code == 400

    conn = sqlite3.connect(db_path)
    status = conn.execute(
        "SELECT order_status FROM orders WHERE id = ?", (order_ids[0],)
    ).fetchone()[0]
    conn.close()
    assert status == "in cart"


def test_unknown_get_parameters_are_not_treated_as_filters(invoice_client):
    client, _, _ = invoice_client
    page = client.get("/submitted?unrelated=value")
    match = re.search(rb'data-filter-state="([^"]+)"', page.data)
    state = json.loads(html.unescape(match.group(1).decode()))
    assert "unrelated" not in state


def test_filter_choices_are_faceted_by_current_results(invoice_client):
    client, _, _ = invoice_client
    page = client.get("/submitted?filter_order_status=awaiting+order")
    match = re.search(rb'data-filter-choices="([^"]+)"', page.data)
    choices = json.loads(html.unescape(match.group(1).decode()))
    vendor_labels = {choice["label"] for choice in choices["vendor_id"]}
    assert "Mouser" in vendor_labels
    assert "DigiKey" not in vendor_labels


def test_existing_submitted_fulfillment_status_is_migrated(tmp_path, monkeypatch):
    db_path = str(tmp_path / "legacy-submitted-status.db")
    monkeypatch.setattr(app_module, "DB_PATH", db_path)
    app_module.init_db()

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO orders (user_email, status, order_status) VALUES (?, ?, ?)",
        ("buyer@lab.org", "submitted", "submitted"),
    )
    conn.commit()
    conn.close()

    app_module.init_db()

    conn = sqlite3.connect(db_path)
    lifecycle_status, order_status = conn.execute(
        "SELECT status, order_status FROM orders"
    ).fetchone()
    conn.close()
    assert lifecycle_status == "submitted"
    assert order_status == "awaiting order"


def test_filter_popup_has_two_working_clear_actions():
    script = (app_module.BASE_DIR + "/static/app.js")
    with open(script, encoding="utf-8") as source:
        javascript = source.read()
    assert 'makePopupBtn("Clear all Filters"' in javascript
    assert 'makePopupBtn("clear this filter"' in javascript
    assert 'makePopupBtn("Clear", ""' not in javascript
