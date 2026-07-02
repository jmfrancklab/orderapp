"""Quote-link handling: turn a Dropbox or SharePoint/OneDrive share link into
the quote PDF behind it, and pull the vendor out of the quote text.

Dropbox and Microsoft links are fundamentally different animals and are kept
separate throughout:

* Dropbox share links are anonymously fetchable: rewriting ``dl=0 -> dl=1``
  (works for both /s/ and /scl/fi/ style links) yields the raw file.
* SharePoint (org tenant, ``*.sharepoint.com``) links honour ``?download=1``
  only when the share is "anyone with the link"; otherwise they bounce to an
  AAD login page. Once the app authenticates against Entra ID, this path
  should switch to a Graph API call with the user's token.
* Personal OneDrive (``1drv.ms`` / ``onedrive.live.com``) uses the public
  shares API: ``https://api.onedrive.com/v1.0/shares/u!<b64url>/root/content``.
"""
import base64
import io
import re
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import requests
from pypdf import PdfReader


class QuoteError(Exception):
    """User-facing problem while retrieving or reading a quote."""


# ------------------------------------------------------------------ classify

def classify_link(url):
    """Return 'dropbox', 'sharepoint', 'onedrive', or None (ordinary link)."""
    host = (urlparse(url if "://" in url else "https://" + url).hostname or "").lower()
    if host.endswith("dropbox.com") or host.endswith("dropboxusercontent.com"):
        return "dropbox"
    if host.endswith("sharepoint.com"):
        return "sharepoint"
    if host == "1drv.ms" or host.endswith("onedrive.live.com"):
        return "onedrive"
    return None


# ------------------------------------------------------------------ direct URLs

def _set_query(url, **params):
    parts = urlparse(url)
    q = dict(parse_qsl(parts.query))
    q.update(params)
    return urlunparse(parts._replace(query=urlencode(q)))


def dropbox_direct(url):
    """Dropbox: dl=1 turns any share link into a direct download."""
    return _set_query(url, dl="1")


def sharepoint_direct(url):
    """Org SharePoint: download=1 works for anonymous ('anyone') shares."""
    return _set_query(url, download="1")


def onedrive_direct(url):
    """Personal OneDrive: encode the share URL for the public shares API."""
    b64 = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    return f"https://api.onedrive.com/v1.0/shares/u!{b64}/root/content"


_DIRECT = {"dropbox": dropbox_direct,
           "sharepoint": sharepoint_direct,
           "onedrive": onedrive_direct}

_LOGIN_HINT = {
    "dropbox": ("Dropbox wouldn't hand over the file — the link may be "
                "password-protected or restricted to invited people. Use a "
                "'anyone with the link' share."),
    "sharepoint": ("SharePoint asked for a Microsoft sign-in, so this share is "
                   "restricted to the organization. Until Microsoft sign-in is "
                   "wired into this app, use an 'Anyone with the link' share, "
                   "or enter the vendor by hand."),
    "onedrive": ("OneDrive wouldn't hand over the file — check that the link "
                 "is shared with 'Anyone with the link'."),
}


def fetch_quote_pdf(url, provider):
    """Download the PDF behind a share link. Raises QuoteError with a
    user-facing message on any failure."""
    direct = _DIRECT[provider](url)
    try:
        r = requests.get(direct, timeout=20, allow_redirects=True,
                         headers={"User-Agent": "ACERT-ordering/1.0"})
    except requests.RequestException as e:
        raise QuoteError(f"Couldn't reach {provider}: {e.__class__.__name__}")
    if r.status_code in (401, 403) or not r.content.startswith(b"%PDF"):
        # HTML instead of a PDF almost always means a login/permission wall.
        raise QuoteError(_LOGIN_HINT[provider])
    if r.status_code != 200:
        raise QuoteError(f"{provider} returned HTTP {r.status_code} for the quote link.")
    return r.content


# ------------------------------------------------------------------ extraction

def extract_text(pdf_bytes, max_pages=3):
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception:
        raise QuoteError("The linked file isn't a readable PDF.")
    text = "\n".join((p.extract_text() or "") for p in reader.pages[:max_pages])
    if not text.strip():
        raise QuoteError("The quote PDF has no extractable text "
                         "(likely a scan — OCR isn't wired in yet).")
    return text


_DOMAIN_RE = re.compile(
    r"\b((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(?:com|net|org|io|de|co|us|biz))\b",
    re.I)
_GENERIC_DOMAINS = {"dropbox.com", "sharepoint.com", "onedrive.com", "live.com",
                    "microsoft.com", "gmail.com", "outlook.com", "adobe.com"}


def match_vendor(text, vendors):
    """Match quote text against known vendors by name or website domain.

    ``vendors``: iterable of dicts with 'id', 'name', 'domain'.
    Returns (vendor_dict_or_None, hint_domains). The vendor appearing
    earliest in the text wins (quotes carry their letterhead up top).
    """
    low = text.lower()
    best, best_pos = None, None
    for v in vendors:
        positions = []
        name = v["name"].strip().lower()
        if name:
            i = low.find(name)
            if i >= 0:
                positions.append(i)
        if v.get("domain"):
            i = low.find(v["domain"].lower())
            if i >= 0:
                positions.append(i)
        if positions:
            pos = min(positions)
            if best_pos is None or pos < best_pos:
                best, best_pos = v, pos
    hints = []
    for m in _DOMAIN_RE.finditer(text):
        d = m.group(1).lower()
        d = d[4:] if d.startswith("www.") else d
        if d not in _GENERIC_DOMAINS and d not in hints:
            hints.append(d)
    return best, hints[:5]
