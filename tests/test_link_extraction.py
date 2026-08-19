"""Tests for product-page price extraction and vendor contact-info detection.

All tests run against saved HTML fixtures — no network required.
Fixtures are representative of real site structure:
  mouser_product.html  — Mouser Electronics product page (JSON-LD Product/Offer)
  mouser_home.html     — Mouser homepage (JSON-LD Organization with address)
  ebay_product.html    — real eBay product-group page saved 2026-07-06

Test (1): vendor auto-recognition by domain + contact-info extraction
Test (2): price extraction from local HTML fixtures
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import quotes

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def read_fixture(name):
    with open(os.path.join(FIXTURE_DIR, name), encoding="utf-8") as f:
        return f.read()


# ── Test 1: vendor detection + contact info from homepage HTML ────────────────

class TestVendorFromHtml:
    def test_mouser_name(self):
        html = read_fixture("mouser_home.html")
        info = quotes.extract_vendor_from_html(html, "mouser.com")
        assert info["name"] == "Mouser Electronics"

    def test_mouser_address(self):
        html = read_fixture("mouser_home.html")
        info = quotes.extract_vendor_from_html(html, "mouser.com")
        assert "1000 North Main Street" in info["address"]
        assert "Mansfield" in info["address"]
        assert "TX" in info["address"]
        assert "76063" in info["address"]

    def test_mouser_phone(self):
        html = read_fixture("mouser_home.html")
        info = quotes.extract_vendor_from_html(html, "mouser.com")
        assert info["phone"] is not None
        digits = "".join(c for c in info["phone"] if c.isdigit())
        assert digits.endswith("8003466873") or "3466873" in digits

    def test_mouser_website(self):
        html = read_fixture("mouser_home.html")
        info = quotes.extract_vendor_from_html(html, "mouser.com")
        assert "mouser.com" in info.get("website", "")

    def test_fallback_to_domain_when_no_org(self):
        """Plain HTML with no structured data falls back to the domain."""
        html = "<html><body><p>Hello world</p></body></html>"
        info = quotes.extract_vendor_from_html(html, "example.com")
        assert info["name"] == "example.com"
        assert info["website"] == "example.com"


# ── Test 2: price extraction from local HTML fixtures ─────────────────────────

class TestPriceFromHtml:
    def test_mouser_product_jsonld(self):
        """Mouser product page: JSON-LD Offer.price = 49.59."""
        html = read_fixture("mouser_product.html")
        price = quotes.extract_price_from_html(html)
        assert price is not None
        assert float(price) == pytest.approx(49.59)

    def test_ebay_product_jsonld(self):
        """eBay product page: lowest JSON-LD Offer.price = 29.99."""
        html = read_fixture("ebay_product_minimal.html")
        price = quotes.extract_price_from_html(html)
        assert price is not None
        assert float(price) == pytest.approx(29.99)

    def test_og_price_meta(self):
        """Open Graph og:price:amount fallback."""
        html = ('<html><head>'
                '<meta property="og:price:amount" content="19.99">'
                '</head><body></body></html>')
        assert quotes.extract_price_from_html(html) == "19.99"

    def test_digikey_unit_price(self):
        """DigiKey-style unitPrice JSON attribute."""
        html = '<html><body><div data-unit-price="3.47"></div></body></html>'
        assert quotes.extract_price_from_html(html) == "3.47"

    def test_generic_text_pattern(self):
        """Generic text pattern: 'unit price $12.34'."""
        html = "<html><body><p>Unit price $12.34 each</p></body></html>"
        assert quotes.extract_price_from_html(html) == "12.34"

    def test_returns_none_when_no_price(self):
        html = "<html><body><p>No price information here.</p></body></html>"
        assert quotes.extract_price_from_html(html) is None

    def test_ebay_real_fixture(self):
        """Real eBay HTML saved from the browser contains a valid price."""
        path = os.path.join(FIXTURE_DIR, "ebay_product.html")
        if not os.path.exists(path):
            pytest.skip("Full eBay fixture not present")
        html = read_fixture("ebay_product.html")
        price = quotes.extract_price_from_html(html)
        assert price is not None
        assert float(price) > 0


# ── Real browser-saved fixtures (saved by user from Chrome) ──────────────────

class TestRealPageFixtures:
    """Price extraction from HTML saved directly from the user's browser.
    These are the exact pages the bookmarklet runs on — if extraction fails
    here, the bookmarklet price capture will silently send price=null.
    """

    def _price(self, fixture_name, domain):
        path = os.path.join(FIXTURE_DIR, fixture_name)
        if not os.path.exists(path):
            pytest.skip(f"{fixture_name} not present")
        html = read_fixture(fixture_name)
        entry = quotes.catalog_entry_for(domain)
        return quotes.extract_price_from_html(html, entry)

    def test_ebay_example_price(self):
        price = self._price("ebay_example.html", "ebay.com")
        assert price is not None, "No price found in ebay_example.html"
        assert float(price) == pytest.approx(29.99), f"Expected 29.99, got {price}"

    def test_ebay_bookmarklet_regex_finds_price(self):
        """The raw-HTML scan in the bookmarklet JS must find the eBay price even
        when the page has no Product JSON-LD (eBay /p/ pages have only
        BreadcrumbList + WebPage schemas)."""
        import re
        path = os.path.join(FIXTURE_DIR, "ebay_example.html")
        if not os.path.exists(path):
            pytest.skip("ebay_example.html not present")
        with open(path) as f:
            html = f.read()
        pat = re.compile(r"""['"]price['"]\s*:\s*['"']?([\d]+\.[\d]{2})['"']?""")
        m = pat.search(html)
        assert m is not None, "Bookmarklet regex found no price in ebay_example.html"
        assert float(m.group(1)) == pytest.approx(29.99)

    def test_mouser_example_price(self):
        price = self._price("mouser_example.html", "mouser.com")
        assert price is not None, "No price found in mouser_example.html"
        assert float(price) > 0

    def test_amazon_example_price(self):
        price = self._price("amazon_example.html", "amazon.com")
        assert price is not None, "No price found in amazon_example.html"
        assert float(price) > 0
