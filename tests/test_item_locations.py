"""Received items need confirmed locations, in single and atomic bulk edits."""
import sqlite3

import pytest
import app as app_module
from test_admin_permissions import admin_db


@pytest.fixture
def authorized_db(admin_db):
    client, path = admin_db
    with sqlite3.connect(path) as db:
        db.execute('UPDATE allowed_emails SET expenditure_authorization = 1')
    return client, path


@pytest.mark.parametrize('bulk', [False, True])
def test_received_requires_confirmed_location(authorized_db, bulk):
    client, path = authorized_db
    url = '/api/orders/bulk' if bulk else '/api/orders/1'
    body = {'order_ids': [1]} if bulk else {}
    body['order_status'] = 'received'
    assert client.post(url, json=body).status_code == 400
    body['location'] = '  on top of common desk  '
    assert client.post(url, json=body).status_code == 400
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM order_history').fetchone()[0] == 0
    body['confirm_new_location'] = True
    assert client.post(url, json=body).status_code == 200
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT order_status, location FROM orders').fetchone() == (
            'received', 'on top of common desk')
        assert db.execute("SELECT new_value FROM order_history WHERE field='location'").fetchone()[0] == 'on top of common desk'
    app_module.init_db()
    assert b'on top of common desk' in client.get('/submitted').data
    assert client.post('/api/orders/1', json={'location': ' '}).status_code == 400
    assert client.post(url, json={**({'order_ids': [1]} if bulk else {}), 'order_status': 'in cart'}).status_code == 200
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT location FROM orders').fetchone()[0] == ''
    body.pop('confirm_new_location')
    body['location'] = 'ON TOP OF COMMON DESK'
    assert client.post(url, json=body).status_code == 200
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT location FROM orders').fetchone()[0] == 'on top of common desk'
        assert db.execute('SELECT COUNT(*) FROM locations').fetchone()[0] == 1


def test_location_rejected_before_received_and_bulk_is_atomic(authorized_db):
    client, path = authorized_db
    assert client.post('/api/orders/1', json={'location': 'desk', 'confirm_new_location': True}).status_code == 400
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO orders (id, user_email, status, order_status) VALUES (26, 'user@lab.org', 'submitted', 'received')")
    assert client.post('/api/orders/bulk', json={'order_ids': [1, 26], 'tracker_email': 'user@lab.org'}).status_code == 400
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM trackers').fetchone()[0] == 0
    page = client.get('/submitted').get_data(as_text=True)
    assert '>0x1a</strong>' in page
    assert 'list="location-options"' in page
    assert 'Location required' in page
