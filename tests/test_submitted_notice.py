"""Static coverage for the dismissible Submitted-page ordering guide."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_submitted_page_explains_the_cart_to_invoice_workflow():
    template = (ROOT / "templates" / "submitted.html").read_text()

    assert 'one_time_confirm("submitted-ordering-notice"' in template
    assert "Set every item from the same purchase or invoice" in template
    assert "Mark all in cart as ordered" in template
    assert "invoice and receipt files" in template


def test_notice_dismissal_is_stored_per_browser_user():
    javascript = (ROOT / "static" / "app.js").read_text()

    assert 'notice.dataset.oneTimeConfirm' in javascript
    assert "notice.dataset.currentUser" in javascript
    assert "window.localStorage.getItem(noticeStorageKey)" in javascript
    assert 'window.localStorage.setItem(noticeStorageKey, "1")' in javascript
