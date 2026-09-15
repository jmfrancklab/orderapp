"""Admin authorization and the one-time grant for existing users."""
import sqlite3

import pytest
import app as app_module


@pytest.fixture
def admin_db(tmp_path, monkeypatch):
    path = str(tmp_path / "admin.db")
    monkeypatch.setattr(app_module, "DB_PATH", path)
    app_module.init_db()
    with sqlite3.connect(path) as db:
        db.executemany(
            "INSERT INTO allowed_emails (email, added_at, is_admin) VALUES (?, '', ?)",
            [("admin@lab.org", 1), ("user@lab.org", 0)],
        )
        db.execute("INSERT INTO orders (user_email, status) VALUES ('user@lab.org', 'submitted')")
    with app_module.app.test_client() as client:
        with client.session_transaction() as session:
            session["email"] = "user@lab.org"
        yield client, path


def test_legacy_migration_grants_once(tmp_path, monkeypatch):
    path = str(tmp_path / "legacy.db")
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE allowed_emails (id INTEGER PRIMARY KEY, email TEXT UNIQUE, added_by TEXT, added_at TEXT)")
        db.execute("INSERT INTO allowed_emails (email) VALUES ('existing@lab.org')")
    monkeypatch.setattr(app_module, "DB_PATH", path)
    app_module.init_db()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT is_admin FROM allowed_emails").fetchone() == (1,)
        db.execute("UPDATE allowed_emails SET is_admin = 0")
        db.execute("INSERT INTO allowed_emails (email) VALUES ('new@lab.org')")
    app_module.init_db()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT is_admin FROM allowed_emails").fetchall() == [(0,), (0,)]


def test_nonadmin_denied_without_mutations(admin_db):
    client, path = admin_db
    response = client.post('/projects', data={'name': 'Forbidden'})
    assert response.status_code == 403
    assert b'ERROR -- you do not have permissions to create a project!' in response.data
    assert client.post('/users', data={'email': 'new@lab.org', 'is_admin': '1'}).status_code == 403
    assert client.post('/users/2/admin', data={'is_admin': '1'}).status_code == 403
    assert client.post('/api/orders/1/trackers', json={'email': 'new@lab.org'}).status_code == 403
    assert client.post('/api/orders/bulk', json={
        'order_ids': [1], 'tracker_email': 'new@lab.org', 'order_status': 'in cart',
    }).status_code == 403
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM projects').fetchone() == (0,)
        assert db.execute('SELECT COUNT(*) FROM allowed_emails').fetchone() == (2,)
        assert db.execute('SELECT COUNT(*) FROM trackers').fetchone() == (0,)
        assert db.execute('SELECT is_admin FROM allowed_emails WHERE id=2').fetchone() == (0,)
    assert client.post('/api/orders/1/trackers', json={'email': 'admin@lab.org'}).status_code == 200


def test_admin_can_create_and_manage_permissions(admin_db):
    client, path = admin_db
    with client.session_transaction() as session:
        session['email'] = 'admin@lab.org'
    assert client.post('/projects', data={'name': 'Allowed'}).status_code == 302
    assert client.post('/users', data={'email': 'new@lab.org'}).status_code == 302
    assert client.post('/api/orders/1/trackers', json={'email': 'invited@lab.org'}).status_code == 200
    assert client.post('/api/orders/bulk', json={
        'order_ids': [1], 'tracker_email': 'bulk@lab.org',
    }).status_code == 200
    assert client.post('/users/2/admin', data={'is_admin': '1'}).status_code == 302
    page = client.get('/users').get_data(as_text=True)
    assert '<th>Admin</th>' in page
    assert 'Admin for user@lab.org' in page
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT is_admin FROM allowed_emails WHERE email='new@lab.org'").fetchone() == (0,)
        assert db.execute("SELECT is_admin FROM allowed_emails WHERE id=2").fetchone() == (1,)
    assert client.post('/users/2/admin', data={}).status_code == 302
    with client.session_transaction() as session:
        session['email'] = 'user@lab.org'
    assert client.post('/projects', data={'name': 'Revoked'}).status_code == 403
