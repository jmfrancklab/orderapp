"""Tests for quote PDF text extraction and vendor info parsing.

Known-good values for the Red Barn quote fixture are pinned here.
Phone tests check digits only — the regex accepts several punctuation styles
((607) 772-1888, 607-772-1888, 607.772.1888, etc.) and the phone may appear
anywhere on the page, not necessarily adjacent to the address block.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import quotes
from conftest import xor_crypt

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
REDBARN_ENC = os.path.join(FIXTURE_DIR, "redbarn_quote.pdf.enc")


@pytest.fixture(scope="module")
def redbarn_text():
    with open(REDBARN_ENC, "rb") as f:
        pdf_bytes = xor_crypt(f.read())
    return quotes.extract_text(pdf_bytes)


@pytest.fixture(scope="module")
def redbarn_info(redbarn_text):
    return quotes.extract_vendor_info(redbarn_text)


# ── Red Barn fixture: exact known-good values ─────────────────────────────────

def test_extract_finds_block(redbarn_info):
    assert redbarn_info is not None

def test_extract_vendor_name(redbarn_info):
    assert redbarn_info["name"] == "Red Barn Technology Group, Inc"

def test_extract_street(redbarn_info):
    assert redbarn_info["street"] == "37 Pine St"

def test_extract_city_state_zip(redbarn_info):
    assert redbarn_info["city"]  == "Binghamton"
    assert redbarn_info["state"] == "NY"
    assert redbarn_info["zip"]   == "13901"

def test_extract_address_formatted(redbarn_info):
    # Full single-line form stored in the vendor address field.
    assert redbarn_info["address"] == "37 Pine St, Binghamton, NY 13901"

def test_extract_phone(redbarn_info):
    # Format varies by regex style; pin to digits only.
    assert redbarn_info["phone"] is not None
    digits = "".join(c for c in redbarn_info["phone"] if c.isdigit())
    assert digits == "6077721888"

def test_extract_website(redbarn_info):
    assert redbarn_info["website"] == "thinkredbarn.com"


# ── Price extraction ──────────────────────────────────────────────────────────

def test_extract_price(redbarn_text):
    """Red Barn quote total is $9,576.31 — appears on a separate line from 'Total'."""
    price = quotes.extract_net_price(redbarn_text)
    assert price == "9576.31"

def test_extract_price_inline():
    """Inline label+amount on one line."""
    text = "Grand Total: $1,234.56\n"
    assert quotes.extract_net_price(text) == "1234.56"

def test_extract_price_multiline():
    """Label on one line, amount several lines below (Red Barn PDF style)."""
    text = "Total\nTotal\n$ \n$ \n9,576.31\n9,576.31\n"
    assert quotes.extract_net_price(text) == "9576.31"

def test_extract_price_priority():
    """Grand Total beats Subtotal when both appear."""
    text = "Subtotal\n100.00\nGrand Total\n115.00\n"
    assert quotes.extract_net_price(text) == "115.0"


# ── Regex behaviour: comma vs no-comma city/state separator ──────────────────

def test_city_state_zip_no_comma():
    """Matches "City ST ZIP" (no comma) — the Red Barn PDF format."""
    text = "Red Barn Technology Group, Inc\n37 Pine St\nBinghamton NY 13901\n"
    info = quotes.extract_vendor_info(text)
    assert info is not None
    assert info["city"]  == "Binghamton"
    assert info["state"] == "NY"
    assert info["zip"]   == "13901"

def test_city_state_zip_with_comma():
    """Matches the traditional "City, ST ZIP" format."""
    text = "Acme Corp\n100 Industrial Blvd\nSpringfield, IL 62701\n"
    info = quotes.extract_vendor_info(text)
    assert info is not None
    assert info["city"]  == "Springfield"
    assert info["state"] == "IL"
    assert info["zip"]   == "62701"

def test_single_line_address():
    """Street and city/state/zip on one line still yields state and zip."""
    text = "Widget Co\n456 Commerce Dr, Austin, TX 78701\n"
    info = quotes.extract_vendor_info(text)
    assert info is not None
    assert info["state"] == "TX"
    assert info["zip"]   == "78701"
