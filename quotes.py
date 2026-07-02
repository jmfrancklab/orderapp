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
import difflib
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


# ------------------------------------------------------------------ address extraction

_STREET_RE = re.compile(
    r"\d+\s+\w.{0,60}"
    r"\b(?:St(?:reet)?|Ave(?:nue)?|Rd|Road|Dr(?:ive)?|Blvd|Boulevard|"
    r"Ln|Lane|Way|Ct|Court|Pl(?:ace)?|Cir(?:cle)?|Ste|Suite|"
    r"Hwy|Highway|Pkwy|Parkway)\b",
    re.I)

_CITY_STATE_ZIP_RE = re.compile(
    r"([A-Za-z][A-Za-z .]{1,28}),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)"
)

_PHONE_RE = re.compile(r"(\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4})")

_DATE_RE = re.compile(
    r"\b\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}\b"
    r"|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*"
    r"\s+\d{1,2},?\s+\d{4}\b",
    re.I)


def extract_vendor_info(text):
    """Extract vendor name, address, phone, and website from PDF quote text.

    Searches for US address blocks (City, ST ZIP) and looks at nearby lines
    for a street address and company name. Returns a dict with string fields,
    or None if no address block is found.
    """
    all_lines = text.split('\n')

    # Prefer first 80 lines (letterhead area); fall back to full text
    csz_line_idx = None
    for limit in (min(80, len(all_lines)), len(all_lines)):
        for i, line in enumerate(all_lines[:limit]):
            if _CITY_STATE_ZIP_RE.search(line):
                csz_line_idx = i
                break
        if csz_line_idx is not None:
            break
    if csz_line_idx is None:
        return None

    m_csz = _CITY_STATE_ZIP_RE.search(all_lines[csz_line_idx])
    city = m_csz.group(1).strip()
    state = m_csz.group(2)
    zipcode = m_csz.group(3)

    # Search backward from city/state/zip line for street, then company name
    street = name = None
    for j in range(csz_line_idx - 1, max(csz_line_idx - 6, -1), -1):
        line = all_lines[j].strip()
        if not line:
            continue
        if _STREET_RE.search(line):
            street = line
            for k in range(j - 1, max(j - 5, -1), -1):
                candidate = all_lines[k].strip()
                if candidate and not _DATE_RE.search(candidate) and len(candidate) > 2:
                    name = candidate
                    break
            break

    # Phone from lines near the address block
    nearby_start = max(0, csz_line_idx - 5)
    nearby_end = min(len(all_lines), csz_line_idx + 8)
    nearby_text = '\n'.join(all_lines[nearby_start:nearby_end])
    pm = _PHONE_RE.search(nearby_text)
    phone = pm.group(1) if pm else None

    # Nearest non-generic domain to the address block
    domain_text = '\n'.join(all_lines[max(0, csz_line_idx - 8):csz_line_idx + 8])
    website = None
    for m in _DOMAIN_RE.finditer(domain_text):
        d = m.group(1).lower()
        d = d[4:] if d.startswith("www.") else d
        if d not in _GENERIC_DOMAINS:
            website = d
            break

    return {"name": name, "street": street, "city": city,
            "state": state, "zip": zipcode, "phone": phone, "website": website}


def fuzzy_match_vendors(name, vendors, n=3, cutoff=0.45):
    """Return up to n vendors whose names are similar to the given name.

    Uses SequenceMatcher ratio. Returns vendor dicts with an added 'score'
    key (0–1), sorted descending. cutoff filters out poor matches.
    """
    if not name or not vendors:
        return []
    name_l = name.lower().strip()
    scored = []
    for v in vendors:
        score = difflib.SequenceMatcher(None, name_l, v["name"].strip().lower()).ratio()
        if score >= cutoff:
            scored.append({**v, "score": round(score, 3)})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:n]
