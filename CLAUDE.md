# CLAUDE.md — instructions for Claude Code working in this repo

## Version number
`__version__` lives near the top of `app.py`. **Increment it on every commit**
that changes behaviour visible to the user (bug fixes, new features, UI changes).
Use `major.minor.patch` (e.g. `0.10.1 → 0.10.2`). The value is displayed in
the top-right corner of every page so the deployed version can be confirmed at a
glance after a `git pull` + reload.

## Commit style
- Short imperative subject line (≤72 chars)
- Bullet-point body describing *what* changed and *why*
- Always end with `Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>`

## Tests
Run `python -m pytest tests/ -q` before committing. All tests must pass.
Test files live in `tests/`; fixtures in `tests/fixtures/`.
The Red Barn PDF fixture is XOR-encrypted (`redbarn_quote.pdf.enc`) — see
`tests/conftest.py` for the cipher.

## Vendor catalog
`vendor_catalog.yaml` is the single source of truth for vendor domains, contact
info, and price-extraction config. Edit it — not Python — to add or update
vendors. API keys belong in environment variables (PythonAnywhere WSGI file),
never in the YAML or committed files.

## Deployment target
PythonAnywhere (shared hosting). Constraints that matter:
- No root access; no headless browsers (Playwright won't work there).
- SQLite only; no PostgreSQL.
- Environment variables set in the WSGI file (`/var/www/…_wsgi.py`).
- Deploy: `git push` locally, then `cd ~/orderapp && git pull` +
  `touch /var/www/…_wsgi.py` on PythonAnywhere.

## Key files
| File | Purpose |
|---|---|
| `app.py` | Flask backend — all routes, DB schema, auth |
| `quotes.py` | PDF parsing, price extraction, vendor detection |
| `vendor_catalog.yaml` | Vendor domains, contact info, price-extraction config |
| `config.toml` | Runtime config (`auth_provider`, `microsoft.allowed_domains`) |
| `add_user.py` | Console script to seed the first allowed user |
| `danger_reset_database.py` | Wipe DB (with confirmation) |
| `static/app.js` | Autosave, vendor popup, price fetch |
| `static/style.css` | All styles |
| `templates/` | Jinja2 templates |
| `tests/` | pytest suite (Python only; no browser tests) |
