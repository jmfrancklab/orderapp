"""Expenditure checks cover all status-changing entry points."""
import sqlite3

import pytest
import app as app_module
from test_admin_permissions import admin_db


@pytest.mark.parametrize('status', ['awaiting order', 'in cart', 'ordered', 'received'])
def test_status_changes_denied_atomically(admin_db, status):
    client, path = admin_db
    for url, body in [
        ('/api/orders/1', {'order_status': status, 'description': 'Forbidden'}),
        ('/api/orders/bulk', {'order_ids': [1], 'order_status': status}),
        ('/api/invoices/from-cart', {'order_ids': [1]}),
    ]:
        response = client.post(url, json=body)
        assert response.status_code == 403
        assert response.json['error'] == app_module.EXPENDITURE_PERMISSION_MESSAGE
        assert response.json['code'] == 'expenditure_authorization_required'
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT description FROM orders').fetchone() == ('',)
        assert db.execute('SELECT COUNT(*) FROM invoices').fetchone() == (0,)
        assert db.execute('SELECT COUNT(*) FROM order_history').fetchone() == (0,)
    assert client.post('/api/orders/1', json={'order_status': 'not ready'}).status_code == 200
    assert client.post('/api/orders/bulk', json={
        'order_ids': [1], 'order_status': 'not ready',
    }).status_code == 200


def test_admin_manages_independent_permission(admin_db):
    client, path = admin_db
    endpoint = '/users/2/expenditure-authorization'
    assert client.post(endpoint, data={'expenditure_authorization': '1'}).status_code == 403
    with client.session_transaction() as session:
        session['email'] = 'admin@lab.org'
    assert client.post('/api/orders/1', json={'order_status': 'awaiting order'}).status_code == 403
    assert client.post(endpoint, data={'expenditure_authorization': '1'}).status_code == 302
    with client.session_transaction() as session:
        session['email'] = 'user@lab.org'
    assert client.post('/api/orders/1', json={'order_status': 'awaiting order'}).status_code == 200
    assert client.post('/api/orders/bulk', json={
        'order_ids': [1], 'order_status': 'in cart',
    }).status_code == 200
    assert client.post('/api/invoices/from-cart', json={'order_ids': [1]}).status_code == 200
    with client.session_transaction() as session:
        session['email'] = 'admin@lab.org'
    assert client.post(endpoint, data={}).status_code == 302
    with client.session_transaction() as session:
        session['email'] = 'user@lab.org'
    assert client.post('/api/orders/1', json={'order_status': 'received'}).status_code == 403


@pytest.mark.parametrize('authorized', [0, 1])
def test_all_submissions_start_not_ready(admin_db, authorized):
    client, path = admin_db
    with sqlite3.connect(path) as db:
        db.execute('UPDATE allowed_emails SET expenditure_authorization = ?', (authorized,))
        db.execute("INSERT INTO orders (user_email, status, order_status) VALUES ('user@lab.org', 'draft', 'awaiting order')")
    assert client.post('/orders/submit').status_code == 302
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT status, order_status FROM orders WHERE id=2').fetchone() == ('submitted', 'not ready')
    page = client.get('/submitted').get_data(as_text=True)
    assert 'data-one-time-confirm="project-review-notice"' in page
    assert 'Even if you are the project lead' in page
    assert 'I understand' in page


def test_legacy_expenditure_migration_grants_once(tmp_path, monkeypatch):
    path = str(tmp_path / 'legacy-expenditure.db')
    monkeypatch.setattr(app_module, 'DB_PATH', path)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE allowed_emails (id INTEGER PRIMARY KEY, email TEXT UNIQUE, added_by TEXT, added_at TEXT, is_admin INTEGER DEFAULT 0)')
        db.executemany('INSERT INTO allowed_emails (email, is_admin) VALUES (?, ?)', [('admin@lab.org', 1), ('user@lab.org', 0)])
    app_module.init_db()
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT expenditure_authorization FROM allowed_emails').fetchall() == [(1,), (1,)]
        db.execute('UPDATE allowed_emails SET expenditure_authorization = 0 WHERE id=2')
        db.execute("INSERT INTO allowed_emails (email) VALUES ('new@lab.org')")
    app_module.init_db()
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT expenditure_authorization FROM allowed_emails').fetchall() == [(1,), (0,), (0,)]
