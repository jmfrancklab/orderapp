"""ACERT order interface — Flask + SQLite.

Single-file backend (plus quotes.py for Dropbox/SharePoint quote handling).
All state lives in orders.db next to this file (absolute path, so it works
identically under PythonAnywhere's WSGI).
"""
import os
import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlparse

from flask import (Flask, g, jsonify, redirect, render_template, request,
                   session, url_for)

import quotes

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "orders.db")

app = Flask(__name__)
# CHANGE THIS before deploying (any long random string):
app.secret_key = os.environ.get("ORDERAPP_SECRET", "dev-secret-change-me")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# ------------------------------------------------------------------ db

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS vendors (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        website TEXT DEFAULT '',
        phone TEXT DEFAULT '',
        tax_exempt_filed INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS projects (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        notes TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY,
        user_email TEXT NOT NULL,               -- locked after submission
        description TEXT NOT NULL DEFAULT '',
        link TEXT NOT NULL DEFAULT '',
        vendor_id INTEGER REFERENCES vendors(id) ON DELETE SET NULL,
        project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
        use_note TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'draft',   -- 'draft' | 'submitted'
        submitted_at TEXT                       -- locked after submission
    );
    CREATE TABLE IF NOT EXISTS trackers (
        order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
        email TEXT NOT NULL,
        UNIQUE (order_id, email)
    );
    CREATE TABLE IF NOT EXISTS order_history (
        id INTEGER PRIMARY KEY,
        order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
        changed_by TEXT NOT NULL,
        changed_at TEXT NOT NULL,
        field TEXT NOT NULL,
        old_value TEXT,
        new_value TEXT
    );
    """)
    db.commit()
    db.close()


init_db()

# ------------------------------------------------------------------ helpers

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_change(db, order_id, field, old, new):
    """Record one field change in order_history (no GUI for this yet)."""
    db.execute(
        "INSERT INTO order_history (order_id, changed_by, changed_at, field,"
        " old_value, new_value) VALUES (?,?,?,?,?,?)",
        (order_id, current_user(), now_iso(), field,
         None if old is None else str(old),
         None if new is None else str(new)))


def vendor_incomplete(v):
    """A vendor is flagged if website, phone, or tax-exemption filing is missing."""
    return not (v["website"].strip() and v["phone"].strip()
                and v["tax_exempt_filed"])


def domain_of(url_or_site):
    """Bare registrable-ish domain: 'https://www.digikey.com/x' -> 'digikey.com'."""
    s = (url_or_site or "").strip()
    if not s:
        return ""
    if "://" not in s:
        s = "https://" + s
    host = (urlparse(s).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def fetch_vendors(db):
    rows = db.execute("SELECT * FROM vendors ORDER BY name COLLATE NOCASE").fetchall()
    return [dict(r, incomplete=vendor_incomplete(r), domain=domain_of(r["website"]))
            for r in rows]


def fetch_projects(db):
    return db.execute("SELECT * FROM projects ORDER BY name COLLATE NOCASE").fetchall()


def trackers_for(db, order_ids):
    out = {oid: [] for oid in order_ids}
    if order_ids:
        marks = ",".join("?" * len(order_ids))
        for r in db.execute(
                f"SELECT order_id, email FROM trackers WHERE order_id IN ({marks}) ORDER BY email",
                list(order_ids)):
            out[r["order_id"]].append(r["email"])
    return out


def current_user():
    return session.get("email")


def login_required(view):
    from functools import wraps

    @wraps(view)
    def wrapped(*a, **kw):
        if not current_user():
            return redirect(url_for("login"))
        return view(*a, **kw)
    return wrapped


def order_visible_to(db, order_id, email):
    """User may touch an order if they created it or track it."""
    return db.execute(
        """SELECT o.* FROM orders o
           LEFT JOIN trackers t ON t.order_id = o.id AND t.email = ?
           WHERE o.id = ? AND (o.user_email = ? OR t.email IS NOT NULL)""",
        (email, order_id, email)).fetchone()

# ------------------------------------------------------------------ auth (stub)

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if EMAIL_RE.match(email):
            session["email"] = email
            return redirect(url_for("orders"))
        error = "Enter a valid email address."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ------------------------------------------------------------------ pages

@app.route("/")
def index():
    return redirect(url_for("orders"))


@app.route("/orders")
@login_required
def orders():
    db = get_db()
    email = current_user()
    drafts = db.execute(
        "SELECT * FROM orders WHERE user_email = ? AND status = 'draft' ORDER BY id",
        (email,)).fetchall()
    return render_template(
        "orders.html", tab="orders", drafts=drafts,
        vendors=fetch_vendors(db), projects=fetch_projects(db),
        trackers=trackers_for(db, [d["id"] for d in drafts]))


@app.route("/orders/new", methods=["POST"])
@login_required
def new_row():
    db = get_db()
    db.execute("INSERT INTO orders (user_email) VALUES (?)", (current_user(),))
    db.commit()
    return redirect(url_for("orders"))


@app.route("/orders/<int:oid>/delete", methods=["POST"])
@login_required
def delete_row(oid):
    db = get_db()
    db.execute("DELETE FROM orders WHERE id = ? AND user_email = ? AND status = 'draft'",
               (oid, current_user()))
    db.commit()
    return redirect(url_for("orders"))


@app.route("/orders/submit", methods=["POST"])
@login_required
def submit_orders():
    db = get_db()
    ts = now_iso()
    ids = [r["id"] for r in db.execute(
        "SELECT id FROM orders WHERE user_email = ? AND status = 'draft'",
        (current_user(),))]
    for oid in ids:
        log_change(db, oid, "status", "draft", "submitted")
    db.execute(
        "UPDATE orders SET status = 'submitted', submitted_at = ? "
        "WHERE user_email = ? AND status = 'draft'",
        (ts, current_user()))
    db.commit()
    return redirect(url_for("submitted"))


@app.route("/submitted")
@login_required
def submitted():
    db = get_db()
    email = current_user()
    rows = db.execute(
        """SELECT DISTINCT o.* FROM orders o
           LEFT JOIN trackers t ON t.order_id = o.id
           WHERE o.status = 'submitted' AND (o.user_email = ? OR t.email = ?)
           ORDER BY o.submitted_at DESC, o.id DESC""",
        (email, email)).fetchall()
    return render_template(
        "submitted.html", tab="submitted", rows=rows,
        vendors=fetch_vendors(db), projects=fetch_projects(db),
        trackers=trackers_for(db, [r["id"] for r in rows]))


@app.route("/vendors", methods=["GET", "POST"])
@login_required
def vendors():
    db = get_db()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if name:
            db.execute(
                "INSERT OR IGNORE INTO vendors (name, website, phone, tax_exempt_filed) "
                "VALUES (?,?,?,?)",
                (name, request.form.get("website", "").strip(),
                 request.form.get("phone", "").strip(),
                 1 if request.form.get("tax_exempt_filed") else 0))
            db.commit()
        return redirect(url_for("vendors"))
    return render_template("vendors.html", tab="vendors", vendors=fetch_vendors(db))


@app.route("/vendors/<int:vid>/update", methods=["POST"])
@login_required
def update_vendor(vid):
    db = get_db()
    db.execute(
        "UPDATE vendors SET name=?, website=?, phone=?, tax_exempt_filed=? WHERE id=?",
        (request.form.get("name", "").strip(),
         request.form.get("website", "").strip(),
         request.form.get("phone", "").strip(),
         1 if request.form.get("tax_exempt_filed") else 0, vid))
    db.commit()
    return redirect(url_for("vendors"))


@app.route("/projects", methods=["GET", "POST"])
@login_required
def projects():
    db = get_db()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if name:
            db.execute("INSERT OR IGNORE INTO projects (name, notes) VALUES (?,?)",
                       (name, request.form.get("notes", "").strip()))
            db.commit()
        return redirect(url_for("projects"))
    return render_template("projects.html", tab="projects",
                           projects=fetch_projects(db))


@app.route("/projects/<int:pid>/update", methods=["POST"])
@login_required
def update_project(pid):
    db = get_db()
    db.execute("UPDATE projects SET name=?, notes=? WHERE id=?",
               (request.form.get("name", "").strip(),
                request.form.get("notes", "").strip(), pid))
    db.commit()
    return redirect(url_for("projects"))

# ------------------------------------------------------------------ autosave API

# Everything is editable at any time EXCEPT who submitted (user_email) and
# when (submitted_at). Every change is written to order_history.
EDITABLE_FIELDS = {"description", "link", "vendor_id", "project_id", "use_note"}


@app.route("/api/orders/<int:oid>", methods=["POST"])
@login_required
def api_save(oid):
    db = get_db()
    email = current_user()
    order = order_visible_to(db, oid, email)
    if order is None:
        return jsonify(error="not found"), 404
    if order["status"] == "draft" and order["user_email"] != email:
        return jsonify(error="not yours"), 403

    data = request.get_json(silent=True) or {}
    sets, vals = [], []
    for field, value in data.items():
        if field not in EDITABLE_FIELDS:
            continue
        if field in ("vendor_id", "project_id"):
            value = int(value) if str(value).strip() else None
        if value != order[field]:
            log_change(db, oid, field, order[field], value)
            sets.append(f"{field} = ?")
            vals.append(value)
    if sets:
        vals.append(oid)
        db.execute(f"UPDATE orders SET {', '.join(sets)} WHERE id = ?", vals)
        db.commit()
    return jsonify(ok=True)


@app.route("/api/orders/<int:oid>/quote_vendor", methods=["POST"])
@login_required
def api_quote_vendor(oid):
    """The link points at a quote PDF on Dropbox or SharePoint/OneDrive:
    fetch it, read it, and set the vendor from the quote."""
    db = get_db()
    order = order_visible_to(db, oid, current_user())
    if order is None:
        return jsonify(error="not found"), 404

    link = (request.get_json(silent=True) or {}).get("link", "").strip()
    provider = quotes.classify_link(link)
    if provider is None:
        return jsonify(matched=False, message="Not a Dropbox/SharePoint link."), 400
    try:
        pdf_bytes = quotes.fetch_quote_pdf(link, provider)
        text = quotes.extract_text(pdf_bytes)
    except quotes.QuoteError as e:
        return jsonify(matched=False, provider=provider, message=str(e))

    vendor, hints = quotes.match_vendor(text, fetch_vendors(db))
    if vendor is None:
        msg = "Quote read, but no known vendor matched."
        if hints:
            msg += " Domains seen in the quote: " + ", ".join(hints) + \
                   ". Add the vendor on the Vendors tab, then re-enter the link."
        return jsonify(matched=False, provider=provider, message=msg)

    if order["vendor_id"] != vendor["id"]:
        log_change(db, oid, "vendor_id", order["vendor_id"], vendor["id"])
        db.execute("UPDATE orders SET vendor_id = ? WHERE id = ?", (vendor["id"], oid))
        db.commit()
    return jsonify(matched=True, provider=provider,
                   vendor_id=vendor["id"], vendor_name=vendor["name"],
                   incomplete=vendor["incomplete"])


@app.route("/api/orders/<int:oid>/trackers", methods=["POST"])
@login_required
def api_add_tracker(oid):
    db = get_db()
    if order_visible_to(db, oid, current_user()) is None:
        return jsonify(error="not found"), 404
    email = (request.get_json(silent=True) or {}).get("email", "").strip().lower()
    if not EMAIL_RE.match(email):
        return jsonify(error="invalid email"), 400
    cur = db.execute("INSERT OR IGNORE INTO trackers (order_id, email) VALUES (?,?)",
                     (oid, email))
    if cur.rowcount:
        log_change(db, oid, "tracker", None, email)
    db.commit()
    return jsonify(ok=True, email=email)


@app.route("/api/orders/<int:oid>/trackers", methods=["DELETE"])
@login_required
def api_remove_tracker(oid):
    db = get_db()
    if order_visible_to(db, oid, current_user()) is None:
        return jsonify(error="not found"), 404
    email = (request.get_json(silent=True) or {}).get("email", "").strip().lower()
    cur = db.execute("DELETE FROM trackers WHERE order_id = ? AND email = ?",
                     (oid, email))
    if cur.rowcount:
        log_change(db, oid, "tracker", email, None)
    db.commit()
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(debug=True)
