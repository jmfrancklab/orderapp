"""Tests for quote PDF text extraction and vendor info parsing."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import quotes

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
REDBARN_PDF = os.path.join(FIXTURE_DIR, "redbarn_quote.pdf")


@pytest.fixture(scope="module")
def redbarn_text():
    with open(REDBARN_PDF, "rb") as f:
        pdf_bytes = f.read()
    return quotes.extract_text(pdf_bytes)


def test_extract_text_is_nonempty(redbarn_text):
    assert len(redbarn_text.strip()) > 100


def test_extract_vendor_name(redbarn_text):
    info = quotes.extract_vendor_info(redbarn_text)
    assert info is not None
    assert info["name"] == "Red Barn Technology Group, Inc"


def test_extract_street(redbarn_text):
    info = quotes.extract_vendor_info(redbarn_text)
    assert info["street"] == "37 Pine St"


def test_extract_city_state_zip(redbarn_text):
    info = quotes.extract_vendor_info(redbarn_text)
    assert info["city"] == "Binghamton"
    assert info["state"] == "NY"
    assert info["zip"] == "13901"


def test_extract_address_formatted(redbarn_text):
    info = quotes.extract_vendor_info(redbarn_text)
    assert "37 Pine St" in info["address"]
    assert "Binghamton, NY 13901" in info["address"]


def test_extract_phone(redbarn_text):
    info = quotes.extract_vendor_info(redbarn_text)
    assert info["phone"] is not None
    assert "607" in info["phone"]
    assert "772" in info["phone"]


def test_extract_website(redbarn_text):
    info = quotes.extract_vendor_info(redbarn_text)
    assert info["website"] == "thinkredbarn.com"


def test_city_state_zip_regex_no_comma():
    """City/state/zip must match even without a comma separator."""
    text = "Red Barn Technology Group, Inc\n37 Pine St\nBinghamton NY 13901\n"
    info = quotes.extract_vendor_info(text)
    assert info is not None
    assert info["city"] == "Binghamton"
    assert info["state"] == "NY"


def test_city_state_zip_regex_with_comma():
    """City/state/zip must also match the comma-separated format."""
    text = "Acme Corp\n100 Industrial Blvd\nSpringfield, IL 62701\n"
    info = quotes.extract_vendor_info(text)
    assert info is not None
    assert info["city"] == "Springfield"
    assert info["state"] == "IL"
    assert info["zip"] == "62701"


def test_single_line_address():
    """Street and city/state/zip on one line should still extract a name."""
    text = "Widget Co\n456 Commerce Dr, Austin, TX 78701\n"
    info = quotes.extract_vendor_info(text)
    assert info is not None
    assert info["state"] == "TX"
    assert info["zip"] == "78701"
