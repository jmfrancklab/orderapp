"""Invoice reimbursement filters preserve visibility and recover from empty results."""

import html
import json
import sqlite3
import re

import pytest

import app as app_module


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DB_PATH", str(tmp_path / "invoices.db"))
    app_module.init_db()
    with sqlite3.connect(app_module.DB_PATH) as db:
        db.executemany(
            "INSERT INTO invoices "
            "(nickname, reimbursement_status, created_by, created_at) VALUES (?,?,?,?)",
            [
                ("Pending invoice", "requires reimbursement", "buyer@lab.org", "2026-09-01"),
                ("Paid invoice", "reimbursed", "buyer@lab.org", "2026-09-02"),
                ("Card invoice", "madhur cc", "buyer@lab.org", "2026-09-03"),
                ("Private invoice", "requires reimbursement", "other@lab.org", "2026-09-04"),
            ],
        )
    with app_module.app.test_client() as client:
        with client.session_transaction() as session:
            session["email"] = "buyer@lab.org"
        yield client


@pytest.mark.parametrize("selected,expected", [
    ([], ["Pending invoice", "Paid invoice", "Card invoice"]),
    (["requires reimbursement"], ["Pending invoice"]),
    (["reimbursed"], ["Paid invoice"]),
    (["madhur cc"], ["Card invoice"]),
    (["requires reimbursement", "reimbursed"], ["Pending invoice", "Paid invoice"]),
    (["unknown"], []),
])
def test_reimbursement_filter(client, selected, expected):
    response = client.get("/invoices", query_string={"filter_reimbursement_status": selected})
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    for name in ["Pending invoice", "Paid invoice", "Card invoice", "Private invoice"]:
        assert (name in page) == (name in expected)
    state = json.loads(html.unescape(re.search(r'data-filter-state="([^"]+)"', page)[1]))
    assert state["reimbursement_status"]["selected"] == selected
    assert 'data-filter-field="reimbursement_status"' in page
    assert ("No invoices match" in page) == (not expected)
    assert "No invoices yet" not in page
    choices = json.loads(html.unescape(re.search(r'data-filter-choices="([^"]+)"', page)[1]))
    assert {choice["value"] for choice in choices["reimbursement_status"]} == {
        "requires reimbursement", "reimbursed", "madhur cc"
    }
    assert "Pending invoice" in client.get("/invoices").get_data(as_text=True)
