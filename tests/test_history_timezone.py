"""History uses local wall time for searches and absolute time for sorting."""

import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest
from werkzeug.datastructures import MultiDict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as app_module


def history_row(timestamp, row_id=1):
    return dict(id=row_id, changed_at=timestamp, changed_by="buyer@example.org",
                table_name="orders", field="status", old_value="draft",
                new_value="submitted", order_id=None)


@pytest.mark.parametrize("zone, timestamp, expected", [
    ("America/New_York", "2026-09-14T15:29:00+00:00", "2026-09-14 11:29"),
    ("America/New_York", "2026-01-14T15:29:00+00:00", "2026-01-14 10:29"),
    ("America/New_York", "2026-09-14T02:29:00+00:00", "2026-09-13 22:29"),
    ("Asia/Kathmandu", "2026-09-14T15:29:00+00:00", "2026-09-14 21:14"),
    ("UTC", "2026-09-14T15:29:00+00:00", "2026-09-14 15:29"),
    ("invalid/zone", "2026-09-14T15:29:00+00:00", "2026-09-14 15:29"),
    ("", "2026-09-14T15:29:00", "2026-09-14 15:29"),
])
def test_local_time(zone, timestamp, expected):
    original = history_row(timestamp)
    rows, label = app_module.localize_history_rows([original], zone)
    assert rows[0]["changed_at_local"] == expected
    assert rows[0]["changed_at"] == timestamp
    assert "changed_at_local" not in original
    assert label == (zone if zone not in ("", "invalid/zone") else "UTC")


def test_local_filters_facets_and_dst_sorting():
    rows, _ = app_module.localize_history_rows([
        history_row("2026-11-01T05:45:00+00:00", 1),
        history_row("2026-11-01T06:15:00+00:00", 2),
    ], "America/New_York")
    for args in [MultiDict({"filter_changed_at": "01:45"}),
                 MultiDict({"filter_changed_at": r"^2026-11-01 01:45$",
                            "filter_changed_at_regex": "1"})]:
        filters = app_module.history_filters_from_args(args)
        assert [r["id"] for r in app_module.filter_history_rows(rows, filters)] == [1]
        assert app_module.history_filter_choices(rows, filters)["changed_by"] == [
            {"value": "buyer@example.org", "label": "buyer@example.org"}]
    for direction, expected in [("asc", [1, 2]), ("desc", [2, 1])]:
        result = app_module.sort_history_rows(rows, [
            {"field": "changed_at", "direction": direction}])
        assert [r["id"] for r in result] == expected


def test_history_route_local_date_filter(tmp_path, monkeypatch):
    db_path = tmp_path / "history.db"
    monkeypatch.setattr(app_module, "DB_PATH", str(db_path))
    app_module.init_db()
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO order_history "
                     "(changed_at, changed_by, field, new_value) VALUES (?,?,?,?)",
                     ("2026-09-14T02:29:00+00:00", "buyer@example.org", "status", "submitted"))
    with app_module.app.test_client() as client:
        with client.session_transaction() as session:
            session["email"] = "buyer@example.org"
        client.set_cookie("history_timezone", "America%2FNew_York")
        response = client.get("/history?filter_changed_at=2026-09-13")
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert "2026-09-13<br>" in html
        assert ">22:29</span>" in html
        assert "Dates and time filters use America/New_York" in html
        client.set_cookie("history_timezone", "invalid/zone")
        assert "Dates and time filters use UTC" in client.get("/history").get_data(as_text=True)


@pytest.mark.skipif(not shutil.which("node"), reason="Node is unavailable")
def test_browser_timezone_handshake():
    source = (Path(__file__).resolve().parents[1] / "static/app.js").read_text()
    initialization = source.split('  var saveState =', 1)[0] + "})();"
    harness = r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = SOURCE;
function run(incoming, zone, cookiesEnabled = true) {
  let cookie = '', reloads = 0;
  const document = {
    getElementById: () => ({dataset: {browserTimezone: incoming}}),
    get cookie() { return cookie; },
    set cookie(value) { if (cookiesEnabled) cookie = value.split(';')[0]; }
  };
  const location = {protocol: 'https:', search: '?filter_changed_at=01&sort=changed_at:asc',
                    hash: '#history', reload: () => reloads++};
  vm.runInNewContext(source, {document, window: {location},
    Intl: {DateTimeFormat: () => ({resolvedOptions: () => ({timeZone: zone})})}});
  assert.equal(location.search, '?filter_changed_at=01&sort=changed_at:asc');
  assert.equal(location.hash, '#history');
  return reloads;
}
assert.equal(run('', 'America/New_York'), 1);
assert.equal(run('America/New_York', 'America/New_York'), 0);
assert.equal(run('America/New_York', 'Asia/Kathmandu'), 1);
assert.equal(run('', 'America/New_York', false), 0);
assert.equal(run('Unsupported/Zone', 'Unsupported/Zone'), 0);
assert.equal(run('', undefined), 0);
""".replace("SOURCE", json.dumps(initialization))
    subprocess.run(["node", "-e", harness], check=True, capture_output=True, text=True)
