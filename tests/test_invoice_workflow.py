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
            ("buyer@lab.org", "waiting", 4, "submitted", "submitted", "2026-01-01"),
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
    conn.close()
    assert "invoices" in tables
    assert "invoice_id" in order_columns


def test_ordered_cannot_be_selected_directly(invoice_client):
    client, _, order_ids = invoice_client
    response = client.post(f"/api/orders/{order_ids[2]}", json={"order_status": "ordered"})
    assert response.status_code == 400
    assert "in-cart action" in response.get_json()["error"]


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

    conn = sqlite3.connect(db_path)
    invoice = conn.execute(
        "SELECT nickname, invoice_url, receipt_url FROM invoices WHERE id=?",
        (data["invoice_id"],),
    ).fetchone()
    orders = conn.execute(
        "SELECT id, order_status, invoice_id FROM orders ORDER BY id"
    ).fetchall()
    conn.close()

    assert invoice == (str(data["invoice_id"]), "", "")
    assert orders[0][1:] == ("ordered", data["invoice_id"])
    assert orders[1][1:] == ("ordered", data["invoice_id"])
    assert orders[2][1:] == ("submitted", None)
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
        },
    )
    assert response.status_code == 200

    conn = sqlite3.connect(db_path)
    invoice = conn.execute(
        "SELECT nickname, invoice_url, receipt_url FROM invoices"
    ).fetchone()
    conn.close()
    assert invoice == (
        "Mouser August",
        "https://www.dropbox.com/invoice.pdf",
        "https://www.dropbox.com/receipt.pdf",
    )

    page = client.get("/submitted")
    assert page.status_code == 200
    assert b"<span>Invoice</span>" in page.data
    assert b"Mouser August" in page.data
    assert b'data-invoice-url="https://www.dropbox.com/invoice.pdf"' in page.data

    invoice_page = client.get("/invoices")
    assert invoice_page.status_code == 200
    assert b"Mouser August" in invoice_page.data
    expected_filter = f"/submitted?filter_invoice_id={created['invoice_id']}".encode()
    assert expected_filter in invoice_page.data

    filtered = client.get(expected_filter.decode())
    assert f'data-id="{order_ids[0]}"'.encode() in filtered.data
    assert f'data-id="{order_ids[1]}"'.encode() in filtered.data
    assert f'data-id="{order_ids[2]}"'.encode() not in filtered.data


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


def test_unknown_get_parameters_are_not_treated_as_filters(invoice_client):
    client, _, _ = invoice_client
    page = client.get("/submitted?unrelated=value")
    match = re.search(rb'data-filter-state="([^"]+)"', page.data)
    state = json.loads(html.unescape(match.group(1).decode()))
    assert "unrelated" not in state


def test_filter_choices_are_faceted_by_current_results(invoice_client):
    client, _, _ = invoice_client
    page = client.get(
        "/submitted?filter_order_status=submitted&filter_order_status=in+cart"
    )
    match = re.search(rb'data-filter-choices="([^"]+)"', page.data)
    choices = json.loads(html.unescape(match.group(1).decode()))
    vendor_labels = {choice["label"] for choice in choices["vendor_id"]}
    assert "Mouser" in vendor_labels
    assert "DigiKey" not in vendor_labels
