"""Invoice shipping and explanations survive editing, migration and export."""
from io import BytesIO
import sqlite3

import pytest
from openpyxl import load_workbook

import app as app_module


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, 'DB_PATH', str(tmp_path / 'details.db'))
    app_module.init_db()
    with sqlite3.connect(app_module.DB_PATH) as db:
        db.execute("INSERT INTO invoices (id, nickname, created_by, created_at) VALUES (1, 'Workstation', 'buyer@lab.org', '2026-10-27')")
        db.execute("INSERT INTO invoices (id, nickname, created_by, created_at) VALUES (2, 'Private', 'other@lab.org', '2026-10-27')")
        db.execute("INSERT INTO orders (user_email, description, invoice_id, status) VALUES ('buyer@lab.org', 'Workstation', 1, 'submitted')")
    with app_module.app.test_client() as client:
        with client.session_transaction() as session:
            session['email'] = 'buyer@lab.org'
        yield client


def test_edit_history_render_and_export(client):
    explanation = 'Entered as one total, including freight.\nCompare components manually. <review>'
    assert client.post('/api/invoices/1', json={'shipping': '1,234.50', 'explanation': explanation}).status_code == 200
    app_module.init_db()  # Repeated migrations preserve user-entered values.
    with sqlite3.connect(app_module.DB_PATH) as db:
        assert db.execute('SELECT shipping, explanation FROM invoices WHERE id=1').fetchone() == ('1234.50', explanation)
        assert db.execute("SELECT old_value, new_value FROM order_history WHERE field='shipping' AND table_name='invoices'").fetchone() == ('', '1234.50')
    page = client.get('/invoices').get_data(as_text=True)
    assert 'Shipping ($)' in page and 'Explanation' in page
    assert 'data-shipping="1234.50"' in page
    assert '&lt;review&gt;' in page and '<review>' not in page
    response = client.get('/export/invoices.xlsx')
    assert response.status_code == 200
    rows = list(load_workbook(BytesIO(response.data)).active.values)
    values = dict(zip(rows[0], rows[1]))
    assert values['Shipping ($)'] == 1234.5
    assert values['Explanation'] == explanation
    assert client.post('/api/invoices/1', json={'shipping': ''}).status_code == 200


@pytest.mark.parametrize('shipping', ['-1', 'NaN', 'Infinity', 'no', '1.234', '1e9999'])
def test_shipping_validation_is_atomic(client, shipping):
    assert client.post('/api/invoices/1', json={'shipping': shipping, 'explanation': 'must not save'}).status_code == 400
    with sqlite3.connect(app_module.DB_PATH) as db:
        assert db.execute('SELECT shipping, explanation FROM invoices WHERE id=1').fetchone() == ('', '')


def test_private_invoice_cannot_be_edited(client):
    assert client.post('/api/invoices/2', json={'shipping': '2', 'explanation': 'private'}).status_code == 404


def test_old_invoice_schema_gets_empty_fields(tmp_path, monkeypatch):
    path = str(tmp_path / 'legacy.db')
    monkeypatch.setattr(app_module, 'DB_PATH', path)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE invoices (id INTEGER PRIMARY KEY, nickname TEXT, invoice_url TEXT DEFAULT '', receipt_url TEXT DEFAULT '', created_by TEXT, created_at TEXT)")
        db.execute("INSERT INTO invoices (nickname) VALUES ('Legacy')")
    app_module.init_db()
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT shipping, explanation FROM invoices').fetchone() == ('', '')
