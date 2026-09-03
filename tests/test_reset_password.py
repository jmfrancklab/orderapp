"""Tests for the local-password reset helper."""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import reset_password


@pytest.fixture
def password_db(tmp_path, monkeypatch):
    db_path = tmp_path / "orders.db"
    db = sqlite3.connect(db_path)
    db.execute(
        """CREATE TABLE allowed_emails (
            id INTEGER PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT
        )"""
    )
    db.executemany(
        "INSERT INTO allowed_emails (email, password_hash) VALUES (?, ?)",
        [
            ("user@example.com", "existing-password-hash"),
            ("pending@example.com", None),
        ],
    )
    db.commit()
    db.close()
    monkeypatch.setattr(reset_password, "DB_PATH", str(db_path))
    return db_path


def password_hash(db_path, email):
    db = sqlite3.connect(db_path)
    value = db.execute(
        "SELECT password_hash FROM allowed_emails WHERE email = ?", (email,)
    ).fetchone()[0]
    db.close()
    return value


def test_resets_existing_password(password_db, capsys):
    assert reset_password.main(["USER@example.com"]) == 0

    assert password_hash(password_db, "user@example.com") is None
    assert "Password reset for: user@example.com" in capsys.readouterr().out


def test_reports_password_already_reset(password_db, capsys):
    assert reset_password.main(["pending@example.com"]) == 0

    assert password_hash(password_db, "pending@example.com") is None
    assert "already reset" in capsys.readouterr().out


@pytest.mark.parametrize("email", ["not-an-email", "user@example"])
def test_rejects_invalid_email_without_changes(password_db, capsys, email):
    assert reset_password.main([email]) == 1

    assert password_hash(password_db, "user@example.com") == "existing-password-hash"
    assert "Not a valid email address" in capsys.readouterr().err


def test_rejects_unknown_user_without_changes(password_db, capsys):
    assert reset_password.main(["unknown@example.com"]) == 1

    assert password_hash(password_db, "user@example.com") == "existing-password-hash"
    assert "No allowed user found" in capsys.readouterr().err
