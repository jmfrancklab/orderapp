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
import os
import re
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import requests
from pypdf import PdfReader

_HERE = os.path.dirname(os.path.abspath(__file__))

# ------------------------------------------------------------------ vendor catalog

_catalog_cache = None


def _load_catalog():
    global _catalog_cache
    if _catalog_cache is None:
        path = os.path.join(_HERE, "vendor_catalog.yaml")
        try:
            import yaml
            with open(path) as f:
                _catalog_cache = yaml.safe_load(f) or {}
        except Exception:
            _catalog_cache = {}
    return _catalog_cache


def catalog_entry_for(domain):
    """Return the vendor_catalog.yaml entry whose domains list contains *domain*."""
    domain = (domain or "").lower()
    for entry in _load_catalog().get("vendors", []):
        if domain in [d.lower() for d in entry.get("domains", [])]:
            return entry
    return None


# ------------------------------------------------------------------ API price handlers

def _mouser_api_price(part_number, api_key):
    """Mouser Search API v2 — returns unit price string or None."""
    try:
        r = requests.post(
            "https://api.mouser.com/api/v2/search/partnumber",
            params={"apiKey": api_key},
            json={"SearchByPartRequest": {
                "mouserPartNumber": part_number,
                "partSearchOptions": "1",
            }},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        for part in (r.json().get("SearchResults") or {}).get("Parts") or []:
            breaks = part.get("PriceBreaks") or []
            if breaks:
                return _clean_price(breaks[0].get("Price", ""))
    except Exception:
        pass
    return None


def _digikey_api_price(catalog_id, client_id, client_secret):
    """DigiKey Product Search API v4 (OAuth2 client credentials) — returns price or None."""
    try:
        tok = requests.post(
            "https://api.digikey.com/v1/oauth2/token",
            data={"grant_type": "client_credentials",
                  "client_id": client_id,
                  "client_secret": client_secret},
            timeout=10,
        )
        if tok.status_code != 200:
            return None
        token = tok.json().get("access_token")
        if not token:
            return None
        r = requests.get(
            f"https://api.digikey.com/products/v4/search/{catalog_id}/pricing",
            headers={"Authorization": f"Bearer {token}",
                     "X-DIGIKEY-Client-Id": client_id,
                     "X-DIGIKEY-Locale-Site": "US",
                     "X-DIGIKEY-Locale-Language": "en",
                     "X-DIGIKEY-Locale-Currency": "USD"},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        for row in r.json().get("StandardPricing") or []:
            p = _clean_price(str(row.get("UnitPrice", "")))
            if p:
                return p
    except Exception:
        pass
    return None


class QuoteError(Exception):
    """User-facing problem while retrieving or reading a quote."""


# ------------------------------------------------------------------ classify

def _quote_storage_entries():
    """Return catalog entries that have a quote_storage block."""
    return [e for e in _load_catalog().get("vendors", []) if e.get("quote_storage")]


def classify_link(url):
    """Return the quote-storage provider name ('dropbox', 'sharepoint', …) or None.

    Provider mapping is read from vendor_catalog.yaml quote_storage entries so
    new providers can be added without changing Python code.
    """
    host = (urlparse(url if "://" in url else "https://" + url).hostname or "").lower()
    for entry in _quote_storage_entries():
        for domain in entry.get("domains", []):
            d = domain.lower()
            if host == d or host.endswith("." + d):
                return entry["quote_storage"]["provider"]
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


def _login_hint(provider):
    """Return the user-facing login-wall message for a quote-storage provider."""
    for entry in _quote_storage_entries():
        if entry["quote_storage"].get("provider") == provider:
            hint = entry["quote_storage"].get("login_hint", "")
            if hint:
                return hint.strip()
    return f"Could not access the {provider} link — check sharing permissions."


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
        raise QuoteError(_login_hint(provider))
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


# Matches both quoted and unquoted type= values (eBay omits the quotes)
_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']?application/ld\+json["\']?[^>]*>(.*?)</script>',
    re.S | re.I)

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
    r"([A-Za-z][A-Za-z .]{1,28})[\s,]+([A-Za-z]{2})\.?\s+(\d{5}(?:-\d{4})?)"
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
    for a street address and company name. Handles both multi-line letterheads
    and single-line formats ("123 Main St, City, ST 12345"). Returns a dict
    with string fields, or None if no address block is found.
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

    csz_line = all_lines[csz_line_idx].strip()
    m_csz = _CITY_STATE_ZIP_RE.search(csz_line)
    city    = m_csz.group(1).strip()
    state   = m_csz.group(2).upper()   # normalise to uppercase
    zipcode = m_csz.group(3)

    # Determine street: either on the same line before the city/state/zip match,
    # or on the line(s) immediately above.
    street = None
    name_search_from = csz_line_idx  # we'll look for name above this index

    prefix = csz_line[:m_csz.start()].strip().rstrip(',').strip()
    if prefix and _STREET_RE.search(prefix):
        # Single-line case: "123 Main St, Portland, OR 97201"
        street = prefix
        name_search_from = csz_line_idx
    else:
        for j in range(csz_line_idx - 1, max(csz_line_idx - 6, -1), -1):
            line = all_lines[j].strip()
            if not line:
                continue
            if _STREET_RE.search(line):
                street = line
                name_search_from = j
                break

    # Company name: first non-blank, non-date line above the street (or city line)
    name = None
    for k in range(name_search_from - 1, max(name_search_from - 5, -1), -1):
        candidate = all_lines[k].strip()
        if candidate and not _DATE_RE.search(candidate) and len(candidate) > 2:
            name = candidate
            break

    # Build a single-line address string (comma-separated so it reads correctly
    # in an <input> field where newlines would be silently dropped).
    addr_parts = []
    if street:
        addr_parts.append(street)
    addr_parts.append(f"{city}, {state} {zipcode}")
    address = ", ".join(addr_parts)

    # Phone from lines near the address block
    nearby = '\n'.join(all_lines[max(0, csz_line_idx - 5):csz_line_idx + 8])
    pm = _PHONE_RE.search(nearby)
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

    return {"name": name, "street": street, "city": city, "state": state,
            "zip": zipcode, "address": address, "phone": phone, "website": website}


# ------------------------------------------------------------------ price extraction

# Per-line label patterns, checked in priority order
_PRICE_LABEL_PATS = [
    (3, re.compile(r'\b(?:grand\s*total|total\s*due|amount\s*due|balance\s*due|'
                   r'invoice\s*total|total\s*amount\s*due)\b', re.I)),
    (2, re.compile(r'\b(?:net\s*(?:price|total|amount)|'
                   r'total\s*(?:price|amount|cost))\b', re.I)),
    (1, re.compile(r'\b(?:subtotal|sub\s*total|total\s*charges|'
                   r'order\s*total|untaxed\s*amount)\b', re.I)),
]
# Plain "Total" alone on a line (PDFs often put label and value on separate lines)
_TOTAL_ONLY_RE = re.compile(r'^\s*total\s*$', re.I)
# Dollar amount (with or without commas)
_AMOUNT_RE = re.compile(r'(?<!\d)([\d,]+\.\d{2})(?!\d)')


def extract_net_price(text):
    """Return the net/total price from a quote PDF as '1234.56', or None.

    Handles both inline format ("Total: $1,234.56") and multi-line PDF layouts
    where the label and dollar amount appear on separate lines (scans up to 5
    lines ahead of the label).
    """
    lines = text.split('\n')
    n = len(lines)
    best_priority, best_val = -1, None

    for i, line in enumerate(lines):
        priority = -1
        for p, pat in _PRICE_LABEL_PATS:
            if pat.search(line):
                priority = p
                break
        # Plain "Total" on its own line → treat as a grand total
        if priority < 0 and _TOTAL_ONLY_RE.match(line):
            priority = 2

        if priority < 0:
            continue

        # Search this line plus the next 4 for the first positive dollar amount
        window = '\n'.join(lines[i:min(i + 5, n)])
        for m in _AMOUNT_RE.finditer(window):
            raw = m.group(1).replace(',', '')
            try:
                f = float(raw)
                if f > 0 and priority > best_priority:
                    best_priority, best_val = priority, f
                    break
            except ValueError:
                pass

    return str(round(best_val, 2)) if best_val is not None else None


def _clean_price(s):
    s = str(s).replace(",", "").replace("$", "").strip()
    try:
        return str(round(float(s), 2))
    except (ValueError, TypeError):
        return None


def _price_from_jsonld(data):
    """Recursively extract price from a schema.org Product/Offer JSON-LD object."""
    if isinstance(data, list):
        for item in data:
            p = _price_from_jsonld(item)
            if p:
                return p
        return None
    if not isinstance(data, dict):
        return None
    t = data.get("@type", "")
    if isinstance(t, list):
        t = " ".join(t)
    if "Product" in t or "Offer" in t:
        offers = data.get("offers") or data.get("Offers")
        if isinstance(offers, list):
            # Skip null/non-dict entries (eBay puts null as the first element)
            offers = next((o for o in offers if isinstance(o, dict)), None)
        if isinstance(offers, dict):
            price = offers.get("price") or offers.get("lowPrice")
            if price is not None:
                p = _clean_price(price)
                if p:
                    return p
        price = data.get("price")
        if price is not None:
            p = _clean_price(price)
            if p:
                return p
    for v in data.values():
        if isinstance(v, (dict, list)):
            p = _price_from_jsonld(v)
            if p:
                return p
    return None


_BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Accept": ("text/html,application/xhtml+xml,application/xml;"
               "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

_ACCESS_DENIED_RE = re.compile(
    r"access.{0,10}denied|cf-error-title|challenge-running|"
    r"checking.{0,20}browser|blocked",
    re.I)


def fetch_html(url, timeout=15):
    """Fetch a URL and return the HTML text, or None on any failure.

    Tries curl_cffi first (Chrome TLS impersonation — handles many bot checks);
    falls back to requests if curl_cffi is not installed.  Returns None when
    the site blocks the request (403, 503, or an access-denied HTML page).
    """
    try:
        from curl_cffi import requests as _cf
        r = _cf.get(url, impersonate="chrome124", timeout=timeout,
                    allow_redirects=True)
    except ImportError:
        try:
            r = requests.get(url, timeout=timeout, allow_redirects=True,
                             headers=_BROWSER_HEADERS)
        except requests.RequestException:
            return None
    except Exception:
        return None

    if r.status_code != 200:
        return None
    html = r.text
    # Detect bot-detection / access-denied pages served as 200
    if _ACCESS_DENIED_RE.search(html[:2000]):
        return None
    return html


def resolve_redirect(url, timeout=10):
    """Follow HTTP redirects and return the final URL (unchanged on failure).

    Uses HEAD so no body is downloaded.  Handles shortlinks like mou.sr.
    """
    try:
        from curl_cffi import requests as _cf
        r = _cf.head(url, impersonate="chrome124", timeout=timeout,
                     allow_redirects=True)
        return str(r.url)
    except ImportError:
        pass
    except Exception:
        pass
    try:
        r = requests.head(url, timeout=timeout, allow_redirects=True,
                          headers=_BROWSER_HEADERS)
        return str(r.url)
    except Exception:
        return url


def extract_price_from_html(html, catalog_entry=None):
    """Extract a unit/item price from product-page HTML.

    Tries in order: JSON-LD Product/Offer schema, Open Graph price meta,
    DigiKey unitPrice, Amazon priceAmount, catalog extra_patterns, generic text.
    Returns a price string like '12.34', or None.
    """
    import json as _json

    # 1. JSON-LD product schema
    for m in re.finditer(
            _JSONLD_RE, html):
        try:
            data = _json.loads(m.group(1))
            p = _price_from_jsonld(data)
            if p:
                return p
        except Exception:
            pass

    # 2. Open Graph price meta tag
    m = re.search(
        r'<meta[^>]+property=["\']og:price:amount["\'][^>]+content=["\']([0-9,.]+)["\']',
        html, re.I)
    if m:
        p = _clean_price(m.group(1))
        if p:
            return p

    # 3. DigiKey: unit price in inline JS / data attributes
    for pat in (r'"unitPrice"\s*:\s*"?\$?([\d,]+\.\d{2})"?',
                r'data-unit-price=["\']([0-9.]+)["\']'):
        m = re.search(pat, html)
        if m:
            p = _clean_price(m.group(1))
            if p:
                return p

    # 4. Amazon: priceAmount in embedded JSON
    m = re.search(r'"priceAmount"\s*:\s*"?([\d.]+)"?', html)
    if m:
        p = _clean_price(m.group(1))
        if p:
            return p

    # 5. Catalog vendor-specific extra patterns
    if catalog_entry:
        for pat in (catalog_entry.get("price") or {}).get("extra_patterns") or []:
            m = re.search(pat, html)
            if m:
                p = _clean_price(m.group(1))
                if p:
                    return p

    # 6. Generic: dollar amount near price-related text
    text = re.sub(r'<[^>]+>', ' ', html)
    m = re.search(
        r'(?:unit\s+price|price\s+each|your\s+price|item\s+price|list\s+price)'
        r'\s*[:\s]*\$?\s*([\d,]+\.\d{2})',
        text, re.I)
    if m:
        p = _clean_price(m.group(1))
        if p:
            return p

    return None


def _find_organization_in_jsonld(data):
    """Recursively find an Organization (or LocalBusiness) in JSON-LD data."""
    if isinstance(data, list):
        for item in data:
            r = _find_organization_in_jsonld(item)
            if r:
                return r
        return None
    if not isinstance(data, dict):
        return None
    t = data.get("@type", "")
    if isinstance(t, list):
        t = " ".join(t)
    if any(k in t for k in ("Organization", "LocalBusiness", "Corporation")):
        name = data.get("name", "")
        addr = data.get("address", {})
        phone = (data.get("telephone") or
                 (data.get("contactPoint") or {}).get("telephone") or "")
        if isinstance(addr, dict):
            parts = [addr.get("streetAddress", "")]
            city = addr.get("addressLocality", "")
            state = addr.get("addressRegion", "")
            zipc = addr.get("postalCode", "")
            loc = ", ".join(filter(None, [city, state]))
            if zipc:
                loc += " " + zipc
            if loc:
                parts.append(loc)
            address = ", ".join(p for p in parts if p)
        else:
            address = str(addr) if addr else ""
        # Normalise phone: strip leading +1 country code
        phone = re.sub(r'^\+1[-.\s]?', '', phone)
        if name or address:
            return {"name": name, "address": address,
                    "phone": phone, "website": data.get("url", "")}
    for v in data.values():
        if isinstance(v, (dict, list)):
            r = _find_organization_in_jsonld(v)
            if r:
                return r
    return None


def extract_vendor_from_html(html, domain):
    """Extract vendor contact info from a webpage HTML string.

    Checks vendor_catalog.yaml first (static, always accurate), then tries
    JSON-LD Organization schema, then address-block regex on visible text.
    Always returns a dict with keys: name, address, phone, website.
    """
    # Catalog static data — fastest and most reliable
    entry = catalog_entry_for(domain)
    if entry and (entry.get("name") or entry.get("address")):
        return {
            "name":    entry.get("name", domain),
            "address": entry.get("address", ""),
            "phone":   entry.get("phone", ""),
            "website": entry.get("website", domain),
        }

    import json as _json
    for m in re.finditer(
            _JSONLD_RE, html):
        try:
            data = _json.loads(m.group(1))
            org = _find_organization_in_jsonld(data)
            if org:
                org.setdefault("website", domain)
                if not org["website"]:
                    org["website"] = domain
                return org
        except Exception:
            pass

    # Fall back: run address-block extractor on page text
    text = re.sub(r'<[^>]+>', '\n', html)
    info = extract_vendor_info(text)
    if info and (info.get("name") or info.get("address")):
        return {"name": info.get("name") or domain,
                "address": info.get("address") or "",
                "phone": info.get("phone") or "",
                "website": domain}

    return {"name": domain, "address": "", "phone": "", "website": domain}


def fetch_item_price(url):
    """Fetch a product-page URL and return the unit price, or None.

    Tries vendor API (if key configured) before falling back to HTML scraping.
    """
    # Identify vendor from URL domain
    host = (urlparse(url).hostname or "").lower()
    host = re.sub(r'^www\.', '', host)
    entry = catalog_entry_for(host)

    # API-based pricing (reliable, bypasses bot detection)
    if entry:
        price_cfg = entry.get("price") or {}
        handler = price_cfg.get("api_handler")
        part_pat = price_cfg.get("part_from_url", "")
        part_m = re.search(part_pat, url) if part_pat else None

        if handler == "mouser" and part_m:
            key = os.environ.get(price_cfg.get("api_key_env", ""), "")
            if key:
                p = _mouser_api_price(part_m.group(1), key)
                if p:
                    return p

        elif handler == "digikey" and part_m:
            cid = os.environ.get(price_cfg.get("client_id_env", ""), "")
            sec = os.environ.get(price_cfg.get("client_secret_env", ""), "")
            if cid and sec:
                p = _digikey_api_price(part_m.group(1), cid, sec)
                if p:
                    return p

    # HTML scraping fallback
    html = fetch_html(url)
    if html is None:
        return None
    return extract_price_from_html(html, entry)


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
