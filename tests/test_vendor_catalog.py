"""Tests for vendor_catalog.toml loading and catalog-driven extraction.

All tests run offline — no HTTP required.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import quotes

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


# ── Catalog lookup ────────────────────────────────────────────────────────────

class TestCatalogLookup:
    def test_mouser_by_domain(self):
        e = quotes.catalog_entry_for("mouser.com")
        assert e is not None
        assert e["name"] == "Mouser Electronics"

    def test_mouser_by_shortlink_domain(self):
        """mou.sr is listed as a Mouser domain for shortlink resolution."""
        e = quotes.catalog_entry_for("mou.sr")
        assert e is not None
        assert e["id"] == "mouser"

    def test_digikey(self):
        e = quotes.catalog_entry_for("digikey.com")
        assert e is not None
        assert e["name"] == "DigiKey Electronics"

    def test_fisher(self):
        e = quotes.catalog_entry_for("fishersci.com")
        assert e is not None
        assert "Fisher" in e["name"]

    def test_trc(self):
        e = quotes.catalog_entry_for("trc-canada.com")
        assert e is not None
        assert e["name"] == "Toronto Research Chemicals"

    def test_sigma(self):
        e = quotes.catalog_entry_for("sigmaaldrich.com")
        assert e is not None
        assert "Sigma" in e["name"] or "Millipore" in e["name"]

    def test_sigma_alt_domain(self):
        e = quotes.catalog_entry_for("sigma-aldrich.com")
        assert e is not None
        assert e["id"] == "sigma"

    def test_ebay(self):
        e = quotes.catalog_entry_for("ebay.com")
        assert e is not None

    def test_unknown_domain_returns_none(self):
        assert quotes.catalog_entry_for("example.com") is None

    def test_case_insensitive(self):
        assert quotes.catalog_entry_for("Mouser.COM") is not None


# ── Catalog-driven vendor info (no HTTP) ─────────────────────────────────────

class TestVendorInfoFromCatalog:
    """extract_vendor_from_html should return catalog data without fetching."""

    def _info(self, domain):
        # Pass empty HTML — catalog should answer before HTML is parsed
        return quotes.extract_vendor_from_html("", domain)

    def test_mouser_name(self):
        assert self._info("mouser.com")["name"] == "Mouser Electronics"

    def test_mouser_address(self):
        addr = self._info("mouser.com")["address"]
        assert "Mansfield" in addr and "TX" in addr

    def test_mouser_phone(self):
        phone = self._info("mouser.com")["phone"]
        digits = "".join(c for c in phone if c.isdigit())
        assert "8003466873" in digits

    def test_digikey_address(self):
        addr = self._info("digikey.com")["address"]
        assert "Thief River Falls" in addr

    def test_trc_address(self):
        addr = self._info("trc-canada.com")["address"]
        assert "Toronto" in addr

    def test_sigma_website(self):
        assert "sigmaaldrich" in self._info("sigmaaldrich.com")["website"]


# ── Catalog extra_patterns in price extraction ────────────────────────────────

class TestCatalogPricePatterns:
    def test_amazon_extra_pattern(self):
        """Amazon extra_pattern priceAmount catches prices not in JSON-LD."""
        html = '<html><body><span data-foo=\'{"priceAmount":129.99}\'></span></body></html>'
        entry = quotes.catalog_entry_for("amazon.com")
        price = quotes.extract_price_from_html(html, entry)
        assert price is not None
        assert float(price) == pytest.approx(129.99)

    def test_mouser_api_handler_configured(self):
        entry = quotes.catalog_entry_for("mouser.com")
        assert entry["price"]["api_handler"] == "mouser"
        assert "api_key_env" in entry["price"]
        assert "part_from_url" in entry["price"]

    def test_mouser_part_extraction_from_url(self):
        import re
        entry = quotes.catalog_entry_for("mouser.com")
        url = "https://www.mouser.com/ProductDetail/Analog-Devices/DC2645A"
        m = re.search(entry["price"]["part_from_url"], url)
        assert m is not None
        assert m.group(1) == "DC2645A"

    def test_digikey_part_extraction_from_url(self):
        import re
        entry = quotes.catalog_entry_for("digikey.com")
        url = "https://www.digikey.com/en/products/detail/yageo/RC0603FR-07100KL/726888"
        m = re.search(entry["price"]["part_from_url"], url)
        assert m is not None
        assert m.group(1) == "726888"


# ── Quote-storage providers from catalog ─────────────────────────────────────

class TestQuoteStorageCatalog:
    def test_dropbox_has_quote_storage(self):
        e = quotes.catalog_entry_for("dropbox.com")
        assert e is not None
        assert e.get("quote_storage", {}).get("provider") == "dropbox"

    def test_sharepoint_has_quote_storage(self):
        e = quotes.catalog_entry_for("sharepoint.com")
        assert e is not None
        assert e.get("quote_storage", {}).get("provider") == "sharepoint"

    def test_onedrive_has_quote_storage(self):
        e = quotes.catalog_entry_for("1drv.ms")
        assert e is not None
        assert e.get("quote_storage", {}).get("provider") == "onedrive"

    def test_classify_link_dropbox(self):
        assert quotes.classify_link("https://www.dropbox.com/s/abc123/quote.pdf?dl=0") == "dropbox"

    def test_classify_link_sharepoint(self):
        assert quotes.classify_link("https://acme.sharepoint.com/:b:/g/...") == "sharepoint"

    def test_classify_link_onedrive(self):
        assert quotes.classify_link("https://1drv.ms/b/s!abc") == "onedrive"

    def test_classify_link_purchase_page_is_none(self):
        assert quotes.classify_link("https://www.mouser.com/ProductDetail/x") is None

    def test_classify_link_ebay_is_none(self):
        assert quotes.classify_link("https://www.ebay.com/p/1701411334") is None

    def test_login_hint_from_catalog(self):
        hint = quotes._login_hint("dropbox")
        assert "anyone with the link" in hint.lower()

    def test_quote_storage_domains_list(self):
        """All three quote-storage providers have domains in the catalog."""
        entries = quotes._quote_storage_entries()
        providers = {e["quote_storage"]["provider"] for e in entries}
        assert "dropbox" in providers
        assert "sharepoint" in providers
        assert "onedrive" in providers


# ── Resilience: missing catalog file ─────────────────────────────────────────

def test_catalog_missing_file_graceful():
    """A missing toml file returns {} without raising — catalog disabled silently."""
    orig = quotes._HERE
    quotes._catalog_cache = None   # reset so it tries to reload
    quotes._HERE = "/nonexistent/path/xyz"
    try:
        result = quotes._load_catalog()
        assert result == {}
        assert quotes.catalog_entry_for("mouser.com") is None
    finally:
        quotes._HERE = orig
        quotes._catalog_cache = None   # reload real catalog for subsequent tests
