# ACERT order interface

Flask + SQLite. One file of backend (`app.py`), server-rendered pages, ~150 lines
of dependency-free vanilla JS (autosave, vendor auto-detect, tracker chips).

## What it does

- **Login gate** — email only for now; swap the `/login` route for MS AD later
  (everything downstream only uses `session["email"]`, so the auth swap is local).
- **New order tab** — rows of description / link / vendor / project / use /
  trackers. Every keystroke is autosaved (400 ms debounce) to the server, so
  drafts survive logout/login. ＋ adds rows; 🗑 removes one; **Submit order**
  moves all drafts to Submitted.
- **Vendor auto-detect (purchase links)** — when you paste a link and the
  vendor box is still empty, the link's domain is matched against each vendor's
  website domain (subdomains included) and the vendor is filled in and saved.
- **Quote links (Dropbox vs SharePoint/OneDrive)** — a Dropbox or
  SharePoint/OneDrive link in the Link field is treated as a *quote PDF*, not a
  purchase page: the server fetches the PDF and matches its text against the
  vendors table (by name and by website domain; earliest hit in the quote wins,
  since the letterhead is up top). The two providers are handled as genuinely
  different things in `quotes.py`: Dropbox share links become direct downloads
  via `dl=1`; org SharePoint links get `?download=1` (works only for
  'Anyone with the link' shares — auth-walled shares report that Microsoft
  sign-in is needed, which will come with the Entra ID integration); personal
  OneDrive (`1drv.ms`) goes through the public shares API. Success or failure
  is reported in a small note under the link field.
- **Red ?** — appears next to the vendor selector when the chosen vendor is
  missing website, phone, or the tax-exemption-filed checkbox.
- **Trackers** — type an email in the row, press Enter, it becomes a removable
  chip; those people see the order on their own Submitted tab.
- **Submitted tab** — spreadsheet view of everything you submitted or track,
  with the submitted date. Everything remains editable (autosaved) *except* the
  submission date and who submitted it — enforced server-side, not just in the
  UI. Trackers can be added/removed here too.
- **Change history** — every field change, tracker add/remove, and submission
  is logged to the `order_history` table (who, when, field, old, new). No GUI
  for it yet; inspect with
  `sqlite3 orders.db "SELECT * FROM order_history ORDER BY id DESC"`.
- **Vendors / Projects tabs** — the two reference tables behind the dropdowns.

## Run locally

    python3 -m venv venv
    source venv/bin/activate        # Windows: venv\Scripts\activate
    pip install -r requirements.txt
    python app.py                   # http://127.0.0.1:5000

`orders.db` is created next to `app.py` on first run. Delete it to reset.

## Deploy to PythonAnywhere (git workflow — yes, this works well)

PythonAnywhere doesn't accept `git push` directly, but the standard pattern is
push to GitHub, pull on PythonAnywhere. One-time setup:

**1. Put the code on GitHub** (from this directory):

    git init && git add -A && git commit -m "order interface"
    git remote add origin git@github.com:YOURUSER/orderapp.git
    git push -u origin main

**2. On PythonAnywhere** (Consoles → Bash):

    git clone https://github.com/YOURUSER/orderapp.git
    mkvirtualenv --python=python3.12 orderapp
    pip install -r orderapp/requirements.txt

**3. Web tab** → Add a new web app → **Manual configuration** → Python 3.12. Then
on the web app's config page set:

- **Source code:** `/home/YOURUSER/orderapp`
- **Virtualenv:** `/home/YOURUSER/.virtualenvs/orderapp`
- **WSGI configuration file** (click to edit; replace contents with):

      import sys
      sys.path.insert(0, "/home/YOURUSER/orderapp")
      from app import app as application

**4.** Set a real secret key (Web tab → Environment variables, or a `.env`):
`ORDERAPP_SECRET=<long random string>`. Then hit **Reload**.

Because `app.py` computes the SQLite path from its own location, the database
lands in `/home/YOURUSER/orderapp/orders.db` with no config. Add `orders.db` to
`.gitignore` (already done) so pulls never clobber production data.

**Every subsequent deploy:**

    # locally
    git push
    # on PythonAnywhere (bash console)
    cd ~/orderapp && git pull
    touch /var/www/YOURUSER_pythonanywhere_com_wsgi.py   # reloads the app

That `touch` is equivalent to the Reload button, so a deploy is two commands.
(If you later want true one-command deploys, a GitHub Action can call the
PythonAnywhere API to pull + reload — happy to set that up when you need it.)

## Notes / next steps

- **Concurrency:** SQLite + a few lab users is fine. If you ever see
  "database is locked", add `PRAGMA journal_mode=WAL` in `get_db()`.
- **AD auth:** replace `/login` with an MSAL (Azure AD) OAuth flow; store the
  authenticated email in `session["email"]` and nothing else changes.
- **Scanned quotes:** text extraction uses pypdf; image-only (scanned) quotes
  report "no extractable text". If those show up in practice, add OCR
  (pytesseract) behind the same `quotes.extract_text` call.
- **SharePoint after AD auth:** swap `quotes.fetch_quote_pdf`'s SharePoint
  branch for a Graph API call with the signed-in user's token, and auth-walled
  org shares start working too.
