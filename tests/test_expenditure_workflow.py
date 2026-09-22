"""YAML settings and source-specific permission enforcement."""
from pathlib import Path
import sqlite3

import pytest

import app as app_module
import expenditure_workflow as workflow
from test_admin_permissions import admin_db


URL = "/users/expenditure-workflow"


def form(rules):
    return {f"rule_{i}": "required" if rules[source][target] else "not_required"
            for i, (source, target) in enumerate(workflow.PAIRS)}


def login(client, email):
    with client.session_transaction() as session:
        session["email"] = email


def test_settings_access_save_audit_and_restart(admin_db):
    client, path = admin_db
    page = client.get(URL).get_data(as_text=True)
    assert page.count("<tr>") == 21
    assert '<select' not in page
    assert URL in client.get('/users').get_data(as_text=True)
    rules = workflow.read(app_module.workflow_path())
    assert len(workflow.PAIRS) == 20
    assert rules == workflow.default_rules()
    assert client.post(URL, data=form(rules)).status_code == 403
    login(client, 'admin@lab.org')
    assert client.get(URL).get_data(as_text=True).count('<select') == 20
    rules['ordered']['received'] = False
    assert client.post(URL, data=form(rules)).status_code == 302
    app_module.init_db()
    assert workflow.read(app_module.workflow_path()) == rules
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT changed_by, field, old_value, new_value FROM order_history "
                          "WHERE table_name='expenditure_workflow'").fetchall() == [
            ('admin@lab.org', 'ordered → received', 'required', 'not required')]
    with client.session_transaction() as session:
        session.clear()
    assert client.get(URL).status_code == 302


@pytest.mark.parametrize('change', ['missing', 'invalid', 'extra'])
def test_invalid_form_does_not_write(admin_db, change):
    client, _ = admin_db
    login(client, 'admin@lab.org')
    path = Path(app_module.workflow_path())
    original = path.read_bytes()
    data = form(workflow.default_rules())
    if change == 'missing':
        del data['rule_0']
    elif change == 'invalid':
        data['rule_0'] = 'yes'
    else:
        data['rule_20'] = 'required'
    assert client.post(URL, data=data).status_code == 400
    assert path.read_bytes() == original


@pytest.mark.parametrize('email', ['user@lab.org', 'admin@lab.org'])
@pytest.mark.parametrize('authorized', [0, 1])
def test_source_specific_rules_and_atomic_bulk(admin_db, email, authorized):
    client, path = admin_db
    login(client, email)
    rules = workflow.default_rules()
    rules['awaiting order']['in cart'] = False
    workflow.save(app_module.workflow_path(), rules)
    with sqlite3.connect(path) as db:
        db.execute('UPDATE allowed_emails SET expenditure_authorization=?', (authorized,))
        db.execute("UPDATE orders SET order_status='awaiting order'")
        db.execute("INSERT INTO orders (user_email, status, order_status) "
                   "VALUES ('user@lab.org', 'submitted', 'not ready')")
    response = client.post('/api/orders/bulk', json={'order_ids': [1, 2], 'order_status': 'in cart'})
    assert response.status_code == (200 if authorized else 403)
    if not authorized:
        with sqlite3.connect(path) as db:
            assert db.execute('SELECT order_status FROM orders ORDER BY id').fetchall() == [
                ('awaiting order',), ('not ready',)]
            assert db.execute('SELECT COUNT(*) FROM order_history').fetchone() == (0,)
    assert client.post('/api/orders/1', json={'order_status': 'in cart'}).status_code == 200
    # Resubmitting an unchanged status is not a transition.
    assert client.post('/api/orders/1', json={'order_status': 'in cart', 'description': 'edited'}).status_code == 200


def test_cart_permission_controls_invoice_creation(admin_db):
    client, path = admin_db
    with sqlite3.connect(path) as db:
        db.execute("UPDATE orders SET order_status='in cart'")
    assert client.post('/api/invoices/from-cart', json={'order_ids': [1]}).status_code == 403
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM invoices').fetchone() == (0,)
        assert db.execute('SELECT COUNT(*) FROM order_history').fetchone() == (0,)
    rules = workflow.default_rules()
    rules['in cart']['ordered'] = False
    workflow.save(app_module.workflow_path(), rules)
    assert client.post('/api/orders/1', json={'order_status': 'ordered'}).status_code == 400
    assert client.post('/api/orders/bulk', json={'order_ids': [1], 'order_status': 'ordered'}).status_code == 400
    assert client.post('/api/invoices/from-cart', json={'order_ids': [1]}).status_code == 200


@pytest.mark.parametrize('contents', ['[broken', 'version: 1\ntransitions: {}',
                                     '!!python/object:builtins.object {}'])
def test_invalid_yaml_blocks_transitions_not_ordinary_edits(admin_db, contents):
    client, _ = admin_db
    Path(app_module.workflow_path()).write_text(contents)
    assert client.get(URL).status_code == 503
    assert client.post('/api/orders/1', json={'order_status': 'awaiting order'}).status_code == 503
    assert client.post('/api/orders/1', json={'description': 'still editable'}).status_code == 200
    app_module.init_db()
    assert Path(app_module.workflow_path()).read_text() == contents


def test_failed_replacement_preserves_file_and_history(admin_db, monkeypatch):
    client, path = admin_db
    login(client, 'admin@lab.org')
    original = Path(app_module.workflow_path()).read_bytes()
    def fail_replace(*args):
        raise PermissionError('read only')
    monkeypatch.setattr(workflow.os, 'replace', fail_replace)
    rules = workflow.default_rules()
    rules['not ready']['awaiting order'] = False
    assert client.post(URL, data=form(rules)).status_code == 503
    assert Path(app_module.workflow_path()).read_bytes() == original
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM order_history').fetchone() == (0,)


@pytest.mark.parametrize('value', ['false', 0, None])
def test_yaml_requires_boolean_values(value):
    rules = workflow.default_rules()
    rules['not ready']['awaiting order'] = value
    with pytest.raises(workflow.WorkflowError):
        workflow.validate({'version': 1, 'transitions': rules})


@pytest.mark.parametrize('source,target', workflow.PAIRS)
def test_each_transition_uses_its_saved_rule(admin_db, source, target):
    _, _path = admin_db
    rules = workflow.default_rules()
    orders = [{'order_status': source}]
    with app_module.app.test_request_context('/'):
        app_module.session['email'] = 'user@lab.org'
        db = app_module.get_db()
        rules[source][target] = True
        workflow.save(app_module.workflow_path(), rules)
        assert app_module.check_expenditure_transitions(db, orders, target)[1] == 403
        rules[source][target] = False
        workflow.save(app_module.workflow_path(), rules)
        assert app_module.check_expenditure_transitions(db, orders, target) is None


def test_unreadable_configuration_blocks_even_authorized_users(admin_db, monkeypatch):
    client, path = admin_db
    with sqlite3.connect(path) as db:
        db.execute('UPDATE allowed_emails SET expenditure_authorization=1')
    def unavailable(*args):
        raise workflow.WorkflowError('unreadable')
    monkeypatch.setattr(workflow, 'read', unavailable)
    response = client.post('/api/orders/bulk', json={
        'order_ids': [1], 'order_status': 'awaiting order'})
    assert response.status_code == 503
    assert response.json['code'] == 'expenditure_workflow_unavailable'
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT order_status FROM orders').fetchone() == ('not ready',)
