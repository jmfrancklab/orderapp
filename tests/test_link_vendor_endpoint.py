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
    r = c.post("/api/vendors",
               json={"name": "Mouser Electronics", "website": "mouser.com"},
               content_type="application/json")
    assert r.status_code == 200
    vid = r.get_json()["id"]

    data = link_vendor(client, "https://www.mouser.com/ProductDetail/Analog-Devices/DC2645A")
    assert data["matched"] is True
    assert data["vendor_id"] == vid
    assert data["vendor_name"] == "Mouser Electronics"


# ── Catalog backfill: placeholder name replaced by catalog name ───────────────

class TestBackfillFromCatalog:
    """When vendor was auto-created with domain as name, api_link_vendor
    must update the DB and return the catalog name."""

    def _add_placeholder(self, client_tuple, name, website):
        c, oid = client_tuple
        r = c.post("/api/vendors",
                   json={"name": name, "website": website},
                   content_type="application/json")
        assert r.status_code == 200
        return r.get_json()["id"]

    def test_ebay_placeholder_replaced(self, client):
        """Vendor 'ebay.com' in DB → matched=True, vendor_name='eBay'."""
        self._add_placeholder(client, "ebay.com", "ebay.com")
        data = link_vendor(client, "https://www.ebay.com/p/1701411334")
        assert data["matched"] is True
        assert data["vendor_name"] == "eBay"

    def test_ebay_db_name_updated(self, client):
        """After backfill the DB row must have the catalog name, not 'ebay.com'."""
        import sqlite3, app as app_module
        conn = sqlite3.connect(app_module.DB_PATH)
        row = conn.execute(
            "SELECT name FROM vendors WHERE website='ebay.com'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "eBay"

    def test_mouser_placeholder_replaced(self, client):
        """Vendor 'mouser.com' in DB → matched=True, vendor_name='Mouser Electronics'."""
        self._add_placeholder(client, "mouser.com", "mouser.com")
        data = link_vendor(client, "https://www.mouser.com/ProductDetail/Analog-Devices/DC2645A")
        assert data["matched"] is True
        assert data["vendor_name"] == "Mouser Electronics"
