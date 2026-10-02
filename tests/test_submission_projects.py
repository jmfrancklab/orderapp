"""Project validation rejects the entire draft batch before any mutations."""
import sqlite3

import pytest
from test_admin_permissions import admin_db


@pytest.mark.parametrize("projects", [(None,), (1, None), (1, 999), (1, 1)])
def test_submission_requires_valid_projects_for_every_draft(admin_db, projects):
    client, path = admin_db
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO projects (id, name) VALUES (1, 'Funding')")
        db.executemany(
            "INSERT INTO orders (user_email, project_id, description) VALUES ('user@lab.org', ?, 'Keep me')",
            [(project,) for project in projects],
        )
        # Another user's incomplete draft must not block this user's submission.
        db.execute("INSERT INTO orders (user_email) VALUES ('admin@lab.org')")
        before_orders = db.execute("SELECT * FROM orders").fetchall()
        before_history = db.execute("SELECT * FROM order_history").fetchall()
        before_events = db.execute("SELECT * FROM app_log").fetchall()
    response = client.post('/orders/submit', data={'order_ids': list(range(2, 2 + len(projects)))})
    valid = all(project == 1 for project in projects)
    assert response.status_code == (302 if valid else 400)
    with sqlite3.connect(path) as db:
        if valid:
            assert db.execute("SELECT count(*) FROM orders WHERE user_email='user@lab.org' AND status='draft'").fetchone()[0] == 0
            assert db.execute("SELECT status FROM orders WHERE user_email='admin@lab.org'").fetchone()[0] == 'draft'
        else:
            assert b'Select a project for every selected order before submitting.' in response.data
            assert b'id="submission-error"' in response.data
            assert b'Keep me' in response.data
            assert db.execute("SELECT * FROM orders").fetchall() == before_orders
            assert db.execute("SELECT * FROM order_history").fetchall() == before_history
            assert db.execute("SELECT * FROM app_log").fetchall() == before_events


def test_only_selected_drafts_are_submitted(admin_db):
    client, path = admin_db
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO projects (id, name) VALUES (1, 'Funding')")
        selected = db.execute("INSERT INTO orders (user_email, project_id) VALUES ('user@lab.org', 1)").lastrowid
        untouched = db.execute("INSERT INTO orders (user_email) VALUES ('user@lab.org')").lastrowid
        before = db.execute("SELECT * FROM orders WHERE id=?", (untouched,)).fetchone()
    response = client.post('/orders/submit', data={'order_ids': [selected, selected]})
    assert response.status_code == 302
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT status, order_status FROM orders WHERE id=?", (selected,)).fetchone() == ('submitted', 'not ready')
        assert db.execute("SELECT * FROM orders WHERE id=?", (untouched,)).fetchone() == before
        assert db.execute("SELECT DISTINCT order_id FROM order_history").fetchall() == [(selected,)]
    page = client.get('/orders').data
    assert f'value="{untouched}"'.encode() in page
    assert b'form="submit-orders"' in page


@pytest.mark.parametrize('selection', [[], ['invalid'], ['999'], ['1'], ['3'], ['2', '3']])
def test_invalid_selection_never_mutates_orders(admin_db, selection):
    client, path = admin_db
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO projects (id, name) VALUES (1, 'Funding')")
        db.execute("INSERT INTO orders (user_email, project_id) VALUES ('user@lab.org', 1)")
        db.execute("INSERT INTO orders (user_email, project_id) VALUES ('admin@lab.org', 1)")
        before = {table: db.execute(f'SELECT * FROM {table}').fetchall()
                  for table in ('orders', 'order_history', 'app_log')}
    assert client.post('/orders/submit', data={'order_ids': selection}).status_code == 400
    with sqlite3.connect(path) as db:
        for table, rows in before.items():
            assert db.execute(f'SELECT * FROM {table}').fetchall() == rows
