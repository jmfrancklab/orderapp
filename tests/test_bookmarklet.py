"""Regression tests for vendor-specific bookmarklet behavior."""

import os
import sys
from urllib.parse import unquote


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as app_module


def bookmarklet_source():
    """Return the decoded JavaScript exactly as rendered into the bookmark."""
    with app_module.app.test_request_context("/orders"):
        bookmarklet = app_module.inject_globals()["bookmarklet"]
    assert bookmarklet.startswith("javascript:")
    return unquote(bookmarklet.removeprefix("javascript:"))


class TestMcMasterBookmarklet:
    def test_does_not_use_mcmaster_homepage_metadata(self):
        """McMaster's og:url is its homepage, even on a direct part page."""
        source = bookmarklet_source()
        mcmaster_branch = source.index("if(mcm){")
        metadata_branch = source.index("link[rel=\\\"canonical\\\"]")

        assert "mcmaster\\.com" in source
        assert mcmaster_branch < metadata_branch
        assert "}else{var c=document.querySelector" in source

    def test_uses_selected_inline_order_box_product_link(self):
        """A family-page drawer exposes its selected part through this link."""
        source = bookmarklet_source()

        assert 'a[class*=\\"productDetailLink\\"][href]' in source
        assert 'a[class*=\\"selectedPartNumberLinkOrderBox\\"][href]' in source
        assert "if(mcmPart)url=mcmPart.href" in source

    def test_extracts_selected_part_description_and_price(self):
        source = bookmarklet_source()

        assert '[class*=\\"productDescription\\"]' in source
        assert '[class*=\\"priceCell\\"]' in source
        assert "ph.textContent+' '+mcmPart.textContent" in source
