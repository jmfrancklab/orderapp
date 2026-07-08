"""Integration tests for the /api/orders/<id>/link_vendor endpoint.

These tests exercise the full server-side URL-recognition pipeline:
domain extraction → catalog lookup → vendor info extraction → JSON response.
They run against a real (temporary) SQLite database with no mocking.
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as app_module
from app import app as flask_app


# ── shared fixtures ───────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def client(tmp_path_factory):
    """Flask test client with a seeded temporary database and a logged-in user."""
    tmp = tmp_path_factory.mktemp("linkvendor")
    test_db = str(tmp / "test.db")

    orig_db = app_module.DB_PATH
    app_module.DB_PATH = test_db
    app_module._catalog_cache = None   # reset cache so YAML reloads for test db
    app_module.init_db()

    # Seed: one allowed user + one draft order
    conn = sqlite3.connect(test_db)
    conn.execute("INSERT INTO allowed_emails (email, added_by, added_at) "
                 "VALUES ('tester@lab.org', 'test', '2026-01-01')")
    conn.execute("INSERT INTO orders (user_email) VALUES ('tester@lab.org')")
    conn.commit()
    oid = conn.execute("SELECT id FROM orders").fetchone()[0]
    conn.close()

    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        with c.session_transaction() as sess:
            sess["email"] = "tester@lab.org"
        yield c, oid

    app_module.DB_PATH = orig_db
    import quotes
    quotes._catalog_cache = None   # restore for any subsequent tests


def link_vendor(client_tuple, url):
    c, oid = client_tuple
    r = c.post(f"/api/orders/{oid}/link_vendor",
               json={"link": url},
               content_type="application/json")
    assert r.status_code == 200, r.data
    return r.get_json()


# ── Mouser ───────────────────────────────────────────────────────────────────

class TestMouserRecognition:
    URL = "https://www.mouser.com/ProductDetail/Analog-Devices/DC2645A"

    def test_not_matched_in_db(self, client):
        data = link_vendor(client, self.URL)
        assert data["matched"] is False

    def test_extracted_name(self, client):
        data = link_vendor(client, self.URL)
        assert data["extracted"]["name"] == "Mouser Electronics"

    def test_extracted_address(self, client):
        data = link_vendor(client, self.URL)
        assert "Mansfield" in data["extracted"]["address"]
        assert "TX" in data["extracted"]["address"]

    def test_extracted_phone(self, client):
        data = link_vendor(client, self.URL)
        digits = "".join(c for c in data["extracted"]["phone"] if c.isdigit())
        assert "8003466873" in digits

    def test_hint_domain(self, client):
        data = link_vendor(client, self.URL)
        assert "mouser.com" in data["hint_domains"]


class TestMouserShortlink:
    """mou.sr shortlink should resolve to mouser.com and return the same info."""
    URL = "https://mou.sr/4y6qrsb"

    def test_resolves_to_mouser(self, client):
        data = link_vendor(client, self.URL)
        assert data["matched"] is False
        assert data["extracted"]["name"] == "Mouser Electronics"


# ── eBay ─────────────────────────────────────────────────────────────────────

class TestEbayRecognition:
    URL = "https://www.ebay.com/p/1701411334"

    def test_not_matched_in_db(self, client):
        data = link_vendor(client, self.URL)
        assert data["matched"] is False

    def test_extracted_name(self, client):
        data = link_vendor(client, self.URL)
        assert data["extracted"]["name"] == "eBay"

    def test_extracted_address(self, client):
        data = link_vendor(client, self.URL)
        assert "San Jose" in data["extracted"]["address"]

    def test_hint_domain(self, client):
        data = link_vendor(client, self.URL)
        assert "ebay.com" in data["hint_domains"]


# ── Once vendor is in DB, matched=True ───────────────────────────────────────

def test_matched_after_vendor_added(client):
    """After creating Mouser in the DB, re-submitting the URL returns matched=True."""
    c, oid = client
    # Create vendor
    r = c.post("/api/vendors",
               json={"name": "Mouser Electronics", "website": "mouser.com"},
               content_type="application/json")
    assert r.status_code == 200
    vid = r.get_json()["id"]

    # Now link_vendor should find it
    data = link_vendor(client, "https://www.mouser.com/ProductDetail/Analog-Devices/DC2645A")
    assert data["matched"] is True
    assert data["vendor_id"] == vid
    assert data["vendor_name"] == "Mouser Electronics"
