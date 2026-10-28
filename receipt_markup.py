"""Locate excluded purchases in supporting PDFs without changing the source files."""
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO
import math
import re
import unicodedata
import logging
from pathlib import Path
from threading import Lock

from pypdf import PdfWriter
from pypdf import _cmap
from reportlab.pdfbase.pdfmetrics import stringWidth


def _tokens(text):
    # OCR commonly reads the S in a part number such as SC1030120 as $.
    text = re.sub(r"(?<!\w)\$(?=[A-Za-z]\d)", "S", text)
    return re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKC", text).casefold())


def text_spans(page):
    """Text plus strike endpoints in unscaled PDF coordinates, using its matrices."""
    spans = []
    shown = []
    font_maps = {}

    def before(operator, operands, cm, tm):
        if operator in (b"Tj", b"TJ", b"'", b'"'):
            parts = operands[0] if operator == b"TJ" else [operands[-1]]
            shown.append((list(parts), list(cm), list(tm)))
            del shown[:-64]

    def actual_position(text, font, cm, tm):
        # pypdf's text visitor sometimes supplies stale memo matrices, notably
        # for right-aligned DigiKey price cells. Use the actual show-text operator.
        if not font:
            return cm, tm
        try:
            key = id(font)
            if key not in font_maps:
                # New pypdf releases replaced build_char_map_from_dict with
                # get_encoding. Select the available API without requiring an
                # upgrade/downgrade of the server's existing installation.
                if hasattr(_cmap, "get_encoding"):
                    font_maps[key] = _cmap.get_encoding(font)
                else:
                    font_maps[key] = _cmap.build_char_map_from_dict(200, font)[2:]
            encoding, mapping = font_maps[key]
            target = re.sub(r"\s+", "", text)
            for end in range(len(shown), 0, -1):
                accumulated = ""
                for parts, position_cm, position_tm in reversed(shown[:end]):
                    decoded = ""
                    for part in parts:
                        if isinstance(part, str):
                            decoded += part
                        elif isinstance(part, bytes):
                            if isinstance(encoding, str):
                                raw = part.decode(encoding, "replace")
                            else:
                                raw = "".join(encoding.get(value, chr(value)) for value in part)
                            decoded += "".join(mapping.get(char, char) for char in raw)
                    accumulated = re.sub(r"\s+", "", decoded) + accumulated
                    if accumulated == target:
                        del shown[:end]
                        return position_cm, position_tm
                    if len(accumulated) > len(target):
                        break
        except (TypeError, ValueError, KeyError, UnicodeError):
            pass
        return cm, tm

    def visitor(text, cm, tm, font, size):
        if not text.strip():
            return
        cm, tm = actual_position(text, font, cm, tm)
        x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
        y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
        dx, dy = (tm[0] * cm[0] + tm[1] * cm[2]) * size, (tm[0] * cm[1] + tm[1] * cm[3]) * size
        ux, uy = (tm[2] * cm[0] + tm[3] * cm[2]) * size, (tm[2] * cm[1] + tm[3] * cm[3]) * size
        if math.hypot(dx, dy) < .01 or math.hypot(ux, uy) < .01:
            return
        name = str((font or {}).get("/BaseFont", ""))
        family = ("Courier" if "mono" in name.lower() else
                  "Helvetica-Bold" if "bold" in name.lower() else "Helvetica")
        for line_index, line in enumerate(text.splitlines()):
            line = line.strip()
            if not line:
                continue
            # Native font metrics vary; these endpoints follow the text baseline,
            # with standard-font advance widths for the visible strike length.
            advance = stringWidth(line, family, 1)
            start_x = x + ux * (.3 - line_index * 1.2)
            start_y = y + uy * (.3 - line_index * 1.2)
            box = page.cropbox
            if not (float(box.left) <= start_x <= float(box.right)
                    and float(box.bottom) <= start_y <= float(box.top)):
                continue
            spans.append({"text": line, "height": math.hypot(ux, uy), "line": (start_x, start_y,
                          start_x + advance * dx, start_y + advance * dy)})

    try:
        page.extract_text(visitor_text=visitor, visitor_operand_before=before)
    except Exception:
        # A broken text layer need not prevent embedding an otherwise readable
        # receipt; OCR or the explicit unmatched notice handles this case.
        pass
    return spans


_ocr_lock = Lock()
_ocr_engine = None


def ocr_spans(page):
    """Render and OCR in memory using pip wheels; no system executables or API."""
    global _ocr_engine
    try:
        import pypdfium2 as pdfium
        from tesserocr import PyTessBaseAPI, PSM, RIL, iterate_level
        writer = PdfWriter()
        writer.add_page(page)
        pdf = BytesIO()
        writer.write(pdf)
        # PDFium and a Tesseract API instance must not be shared concurrently.
        # Reuse the English model; neither library needs network I/O.
        with _ocr_lock:
            if _ocr_engine is None:
                _ocr_engine = PyTessBaseAPI(
                    path=str(Path(__file__).with_name("ocr_data")),
                    lang="eng", psm=PSM.AUTO)
            with pdfium.PdfDocument(pdf.getvalue()) as document:
                rendered = document[0]
                # Browser printouts often shrink price tables to tiny text.
                # 360 dpi preserves part numbers and decimal points that the
                # previous 216 dpi pass lost (notably the SigaTek receipt).
                bitmap = rendered.render(scale=min(5, 5000 / max(rendered.get_size())))
                try:
                    image = bitmap.to_pil().copy()
                finally:
                    bitmap.close()
                width, height = image.size
                box = page.cropbox
                sx, sy = float(box.width) / width, float(box.height) / height
                spans = []
                try:
                    _ocr_engine.SetImage(image)
                    _ocr_engine.Recognize()
                    iterator = _ocr_engine.GetIterator()
                    for word in iterate_level(iterator, RIL.WORD) if iterator else ():
                        text = word.GetUTF8Text(RIL.WORD)
                        if not text or not text.strip():
                            continue
                        left, top, right, bottom = word.BoundingBox(RIL.WORD)
                        _, line_top, _, line_bottom = word.BoundingBox(RIL.TEXTLINE)
                        # Words on one OCR line share a baseline, even when a
                        # dollar sign or descender has a different box height.
                        y = float(box.top) - (line_top + (line_bottom-line_top) * .55) * sy
                        spans.append({"text": text.strip(), "confidence": word.Confidence(RIL.WORD),
                                      "height": (bottom-top) * sy,
                                      "line": (float(box.left) + left*sx, y,
                                               float(box.left) + right*sx, y)})
                    # Sparse summary pages can lose the isolated Total label
                    # in AUTO mode. Recover missing labels without duplicating
                    # monetary occurrences or trusting the broken native layer.
                    if (any("untaxed" in s["text"].casefold() for s in spans)
                            and not any(s["text"].casefold().strip(':') == "total" for s in spans)):
                        try:
                            _ocr_engine.SetPageSegMode(PSM.SPARSE_TEXT)
                            _ocr_engine.Recognize()
                            iterator = _ocr_engine.GetIterator()
                            for word in iterate_level(iterator, RIL.WORD) if iterator else ():
                                text = (word.GetUTF8Text(RIL.WORD) or "").strip()
                                if text.casefold().strip(':') not in ("total", "taxes", "tax"):
                                    continue
                                left, top, right, bottom = word.BoundingBox(RIL.WORD)
                                _, line_top, _, line_bottom = word.BoundingBox(RIL.TEXTLINE)
                                y = float(box.top) - (line_top + (line_bottom-line_top) * .55) * sy
                                if not any(s["text"].casefold() == text.casefold() and abs(s["line"][1] - y) < 3 for s in spans):
                                    spans.append({"text": text, "confidence": word.Confidence(RIL.WORD),
                                                  "height": (bottom-top)*sy,
                                                  "line": (float(box.left)+left*sx, y, float(box.left)+right*sx, y)})
                        finally:
                            _ocr_engine.SetPageSegMode(PSM.AUTO)
                finally:
                    _ocr_engine.Clear()
                    image.close()
        return sorted(spans, key=lambda s: (-round(s["line"][1] / 3), s["line"][0]))
    except Exception:
        logging.getLogger(__name__).exception("Receipt OCR failed")
        return []


def receipt_spans(page, data):
    """One OCR pass shared by matching, strikes, dates and coverage checks."""
    if "ocr" not in data:
        data["ocr"] = ocr_spans(page)
    return data["ocr"]


def _anchors(description, protected):
    tokens = _tokens(description)
    candidates = set()
    # Product identifiers are stronger than prose, and survive vendor-specific
    # prefixes, suffixes and shortened descriptions on invoices.
    for token in tokens:
        if len(token) >= 5 and re.search(r"[a-z]", token) and re.search(r"\d", token):
            candidates.add((token,))
    for length in (4, 5, 6):
        for start in range(len(tokens) - length + 1):
            candidates.add(tuple(tokens[start:start + length]))
    if 1 < len(tokens) < 4:
        candidates.add(tuple(tokens))
    # Some vendors omit the maker prefix of a model, e.g. DEV-20359 is
    # printed as 20359 / 1568-20359-ND. Keep numeric suffixes exact.
    candidates.update((m.group(1),) for m in re.finditer(r"\b[a-z]{2,}-(\d{4,})\b", description.casefold()))
    anchors = []
    for candidate in candidates:
        # A short device name such as TLV333 / AD7902 is commonly printed with
        # its package suffix (TLV333IDRCT / AD7902SDZ) on the vendor's receipt.
        suffix = r"[a-z0-9]*" if len(candidate) == 1 and not candidate[0].isdigit() else ""
        pattern = re.compile(r"\b" + r"\s+".join(re.escape(token) for token in candidate) + suffix + r"\b")
        # Never use an anchor also present in this project's purchases.
        if not any(pattern.search(text) for text in protected):
            anchors.append(pattern)
    return anchors


def _matches(spans, patterns):
    tokens, owners = [], []
    for index, span in enumerate(spans):
        words = _tokens(span["text"])
        tokens.extend(words)
        owners.extend([index] * len(words))
    text = " ".join(tokens)
    token_positions = list(re.finditer(r"\S+", text))
    selected = set()
    for pattern in patterns:
        for match in pattern.finditer(text):
            for token, owner in zip(token_positions, owners):
                if token.start() < match.end() and token.end() > match.start():
                    selected.add(owner)
    return selected


# Decimal amounts anywhere; currency/labeled amounts may use zero or one decimal.
_WHOLE_AMOUNT_LABELS = ("shipping", "freight", "insurance", "discount", "tariff", "tax",
                        "subtotal", "total", "unit price", "unit cost", "price", "amount")
_PRICE = re.compile(
    r"(?<![a-zA-Z0-9.])(?:[+-]?\s*[$£€]\s*[+-]?\s*\d[\d,]*(?:\.\d{1,5})?"
    r"|[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2,5})(?![a-zA-Z0-9.])|(?:"
    + "|".join(r"(?<=" + re.escape(label + suffix) + ")"
               for label in _WHOLE_AMOUNT_LABELS for suffix in (" ", ": "))
    + r")[+-]?\d+(?:\.\d)?(?![a-zA-Z0-9.])", re.I)



def _prices(spans):
    prices = []
    for index, span in enumerate(spans):
        text = span["text"]
        matches = list(_PRICE.finditer(text))
        # OCR may put a currency sign or charge label in a separate word box.
        # Recover labeled whole-dollar or single-decimal amounts without treating quantities,
        # phone numbers, or order numbers as money.
        if not matches and re.fullmatch(r"[+-]?\d[\d,]*(?:\.\d)?", text):
            x, y = span["line"][:2]
            left = sorted((s for s in spans if s["line"][2] <= x
                           and abs(s["line"][1]-y) <= span.get("height", 10)*.65),
                          key=lambda s: s["line"][0])
            prefix = " ".join(s["text"] for s in left)
            if re.search(r"(?:[$£€]|\b(?:" + "|".join(_WHOLE_AMOUNT_LABELS) + r"))\s*:?\s*$", prefix, re.I):
                matches = [re.match(r"[+-]?\d[\d,]*(?:\.\d)?", text)]
        width = stringWidth(text, "Helvetica", 1) or 1
        x0, y0, x1, y1 = span["line"]
        for match in matches:
            suffix = text[match.end():]
            if re.match(r"\s*(?:%|units?\b)", suffix, re.I):
                continue
            if match.end() == len(text) and any(
                    re.fullmatch(r"units?", other["text"], re.I)
                    and 0 <= other["line"][0] - x1 < span.get("height", 10) * 3
                    and abs(other["line"][1] - y0) < span.get("height", 10) * .65
                    for other in spans):
                continue
            start = stringWidth(text[:match.start()], "Helvetica", 1) / width
            end = stringWidth(text[:match.end()], "Helvetica", 1) / width
            value = Decimal(re.sub(r"[^0-9.+-]", "", match.group()))
            prices.append({"owner": index, "value": value, "confidence": span.get("confidence", 100), "prefix": text[:match.start()], "line": (
                x0 + (x1-x0)*start, y0 + (y1-y0)*start,
                x0 + (x1-x0)*end, y0 + (y1-y0)*end)})
    return prices


def _row_prices(spans, selected, product_lines, prices):
    """Find amounts beside a matched description, or directly below its wrapped row."""
    result, same_row_result = set(), set()
    for owner in selected:
        span = spans[owner]
        x, y = span["line"][:2]
        height = span.get("height", 10)
        # Other known product titles bound a vertically stacked receipt card.
        lower = max((spans[index]["line"][1] for index in product_lines - selected
                     if spans[index]["line"][1] < y - height), default=-float("inf"))
        upper = min((spans[index]["line"][1] for index in product_lines - selected
                     if spans[index]["line"][1] > y + height), default=float("inf"))
        candidates = [(index, price) for index, price in enumerate(prices)
                      if price["line"][0] >= x - height
                      and lower < price["line"][1] < upper
                      and price["line"][1] <= y + height*2.5
                      and y - price["line"][1] <= height*14]
        same_row = [(index, price) for index, price in candidates
                    if abs(price["line"][1] - y) <= height*.65]
        if same_row:
            same_row_result.update(index for index, _ in same_row)
        elif candidates:
            nearest = max(price["line"][1] for _, price in candidates)
            result.update(index for index, price in candidates
                          if abs(price["line"][1] - nearest) <= height*.65)
    return same_row_result or result


def strike_through(pages, excluded, relevant, geometry=None, costs=None):
    """Strike descriptions AND costs; an exact cost alone is a successful match."""
    if geometry is None:
        geometry = [{"ocr": ocr_spans(page)} for page in pages]
    protected = [" ".join(_tokens(description)) for description in relevant]
    patterns = [_anchors(description, protected) for description in excluded]
    protected_patterns = [_anchors(description, []) for description in relevant]
    lines = [set() for _ in pages]
    found = set()

    def mark(spans, page_index):
        protected_selections = [_matches(spans, anchors) for anchors in protected_patterns]
        protected_lines = set().union(*protected_selections)
        selections = [_matches(spans, anchors) - protected_lines for anchors in patterns]
        product_lines = protected_lines | set().union(*selections)
        prices = _prices(spans)
        protected_prices = set().union(*[
            _row_prices(spans, selected, product_lines, prices) for selected in protected_selections])
        for item_index, selected in enumerate(selections):
            matched_prices = _row_prices(spans, selected, product_lines, prices) - protected_prices
            if costs is not None and not matched_prices:
                # Name differences must not cause a warning when an exact amount
                # can be located. Prefer the extended amount, then the unit cost.
                for wanted in costs[item_index]:
                    exact = [index for index, price in enumerate(prices)
                             if price["value"] == wanted and index not in protected_prices]
                    if exact:
                        matched_prices.add(exact[0])
            lines[page_index].update(spans[index]["line"] for index in selected)
            lines[page_index].update(prices[index]["line"] for index in matched_prices)
            if matched_prices or (costs is None and selected):
                found.add(item_index)

    for page_index, (page, data) in enumerate(zip(pages, geometry)):
        spans = data["ocr"] if "ocr" in data else data.get("native", [])
        mark(spans, page_index)
        if not spans:
            mark(receipt_spans(page, data), page_index)
    return [sorted(page_lines) for page_lines in lines], [
        description for index, description in enumerate(excluded) if index not in found]


def monetary_pages(pages, geometry):
    """Regex every page, retaining occurrence identity and source geometry."""
    result = []
    for page_index, (page, data) in enumerate(zip(pages, geometry)):
        data["verification_issues"] = []
        spans = receipt_spans(page, data)
        if not spans:
            data["verification_issues"].append(
                f"Page {page_index + 1}: OCR could not read this receipt. "
                "Retry with a clearer copy, or compare the database items "
                "with the receipt manually.")
        prices = _prices(spans)
        for index, price in enumerate(prices):
            span = spans[price["owner"]]
            y = price["line"][1]
            height = span.get("height", 10)
            row = sorted((s for s in spans if abs(s["line"][1] - y) <= height * .65),
                         key=lambda s: s["line"][0])
            price.update(id=f"p{page_index + 1}.{index + 1}", page=page_index,
                         context=" ".join(s["text"] for s in row), spans=spans)
        result.append([price for price in prices if price["value"] != 0])
    return result


_SUMMARY_LABELS = (
    ("tax", r"(?:sales\s+tax|estimated\s+tax|\btax(?:es)?\b)"),
    ("discount", r"(?:discount|coupon|savings|promotion)"),
    ("insurance", r"insurance"),
    ("shipping", r"(?:shipping|freight|delivery charge|fedex|\bups\b|\busps\b|\bdhl\b)"),
    ("tariff", r"(?:tariff|duty|surcharge|handling fee|other charge)"),
    ("subtotal", r"(?:sub[ -]?total|merchandise|item\(s\) subtotal)"),
    ("before_tax", r"(?:total before tax|untaxed amount)"),
    ("total", r"(?:grand total|total billed|order total|amount paid|(?<!sub )(?<!sub-)\btotal\b)"),
)


def amount_role(price):
    if price["line"] is None:
        return "item"
    text = price["context"].casefold()
    # A price cell often shares its row with unrelated summary columns. Only
    # the nearest label to its left supplies the role.
    x, y = price["line"][:2]
    left = [s for s in price["spans"] if s["line"][0] <= x
            and abs(s["line"][1] - y) <= s.get("height", 10) * .65]
    text = " ".join(price["prefix"] if s is price["spans"][price["owner"]] else s["text"]
                    for s in sorted(left, key=lambda s: s["line"][0])).casefold()
    if "total before tax" in text:
        return "before_tax"
    if re.search(r"tax(?:es)?\s+on\s*[$£€]?\s*$", text):
        return "taxable_base"
    if re.search(r"shipping[\s-]*(?:including|and|&)", text):
        return "shipping"
    matches = [(m.start(), -rank, role) for rank, (role, pattern) in enumerate(_SUMMARY_LABELS)
               for m in re.finditer(pattern, text)]
    return max(matches)[2] if matches else "item"


def _prorate_shipping(receipt):
    """Distribute the invoice shipping field by project purchase value."""
    source = receipt.setdefault("source_groups", receipt["groups"])
    bases = {key: sum((row[0] for row in rows), Decimal(0))
             for key, rows in source.items()}
    bases = {key: value for key, value in bases.items() if value > 0}
    groups, refs = {}, {}
    for key, rows in source.items():
        for i, row in enumerate(rows):
            target = groups.setdefault(key, [])
            refs[key, len(target)] = (key, i)
            target.append(row)
    base = sum(bases.values(), Decimal(0))
    shipping = Decimal(str(receipt.get("shipping") or "0"))
    if shipping and not bases:
        key = (None, "Unassigned project")
        target = groups.setdefault(key, [])
        refs[key, len(target)] = "shipping"
        target.append((shipping, "Shipping (no positive purchase value to prorate)", 1, shipping))
    if shipping and bases:
        previous, cumulative = Decimal(0), Decimal(0)
        for key, value in bases.items():
            cumulative += value
            rounded = (shipping * cumulative / base).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
            share = rounded - previous
            previous = rounded
            target = groups.setdefault(key, [])
            refs[key, len(target)] = "shipping"
            label = "Shipping" + (" (prorated by project purchase value)" if len(bases) > 1 else "")
            target.append((share, label, 1, share))
    receipt.update(groups=groups, source_refs=refs)


def reconcile(receipt):
    """Keep purchase tuples intact; store source evidence and allocations alongside."""
    geometry = receipt.setdefault("text_geometry", [{} for _ in receipt["pages"]])
    evidence = monetary_pages(receipt["pages"], geometry)
    _prorate_shipping(receipt)
    items = [(key, i, item) for key, rows in receipt["groups"].items() for i, item in enumerate(rows)]
    base = sum((item[0] for _, _, item in items), Decimal(0))
    charges, tax, totals = [], Decimal(0), []
    for page_prices in evidence:
        adjustment_rows = {}
        for price in page_prices:
            role = price["role"] = amount_role(price)
            if role in ("shipping", "insurance", "discount", "tariff", "tax"):
                row_key = (role, round(price["line"][1], 1))
                adjustment_rows.setdefault(row_key, []).append(price)
            if role == "total":
                totals.append(price["value"])
        for (role, _), row in adjustment_rows.items():
            # A charge row may print both unit and extended amounts. Compare
            # only its rightmost extension with the entered invoice shipping.
            price = max(row, key=lambda p: p["line"][0])
            value = -abs(price["value"]) if role == "discount" else price["value"]
            if role == "tax":
                tax += value
            else:
                charges.append((price["id"], role, value))
    # An exact whole-receipt match wins over inferred labels and page totals.
    # OCR can miss an isolated Total label at the end of a multipage receipt.
    exact_totals = [p for row in evidence for p in row
                    if p["value"] == base + tax and p["value"] != 0
                    and p["role"] in ("total", "before_tax")]
    if exact_totals:
        terminal = exact_totals[-1]
        terminal["role"] = "total"
    # Match an otherwise unclaimed amount to entered shipping, to the cent.
    shipping = Decimal(str(receipt.get("shipping") or "0"))
    if shipping and not any(kind == "shipping" for _, kind, _ in charges):
        candidates = [p for row in evidence for p in row
                      if p["role"] == "item" and p["value"] == shipping
                      and not _item_candidates(receipt, p)]
        if candidates:
            price = candidates[-1]
            price["role"] = "shipping"
            charges.append((price["id"], "shipping", shipping))
    # One terminal total per page; multi-order supporting files sum their orders.
    total = sum((next((p["value"] for p in reversed(row) if p["role"] == "total"), Decimal(0))
                 for row in evidence), Decimal(0)) if totals else None
    if exact_totals:
        total = base + tax
    allocations = {(key, i): {"shipping": Decimal(0), "other": Decimal(0)} for key, i, _ in items}
    reimbursable = base
    residual = total - tax - reimbursable if total is not None else None
    receipt.pop("item_candidates", None)
    receipt.update(verification=evidence, charges=charges, excluded_tax=tax,
                   original_total=total, reimbursable_total=total - tax if total is not None else None,
                   allocated_total=reimbursable, unresolved=residual, allocations=allocations)
    return receipt


def verify_amounts(receipt, project=None):
    """Decide each nonzero occurrence, preferring exact totals and shipping."""
    result = []
    charge_refs = {role: [(ref, value) for ref, kind, value in receipt["charges"] if kind == role]
                   for role in ("shipping", "insurance", "discount", "tariff")}
    for page_prices in receipt["verification"]:
        decisions = []
        for price in page_prices:
            role, value = price["role"], price["value"]
            match, item_ref = None, None
            status, reason = "?", "No matching database item was found for this receipt amount."
            if role == "tax":
                status, reason = "strike", "Excluded sales tax; included only in original receipt total"
            elif role == "taxable_base":
                reason = "Taxable purchase amount, not the sales-tax charge. Compare this base with the taxable purchases on the receipt."
            elif role in charge_refs:
                row_refs = [(ref, value) for ref, value in charge_refs[role]
                            if ref.startswith(f'p{price["page"] + 1}.')]
                if role == "shipping":
                    expected = Decimal(str(receipt.get("shipping") or "0"))
                    detected = sum((v for _, kind, v in receipt["charges"] if kind == "shipping"), Decimal(0))
                    reason = (f"Shipping: invoice Shipping field is ${expected:,.2f}; "
                              f"receipt shipping charges total ${detected:,.2f}. "
                              "Enter separate shipping in the invoice, or explain if already included in item costs.")
                    if expected == detected and any(ref == price["id"] for ref, _ in row_refs):
                        status, reason = "ok", f"Shipping matches invoice Shipping field ${expected:,.2f}; prorated by project purchase value."
                    item_ref = "shipping"
                else:
                    match = _item_correspondence(receipt, price)
                    if match:
                        key, index, item, field = match
                        item_ref = (key, index)
                        status = "strike" if project is not None and key != project else "ok"
                        reason = f"{item[1]} ({field}): database ${item[3]:,.2f} x {item[2]} = ${item[0]:,.2f}."
                    else:
                        reason = f"Receipt {role} charge is not separately matched to a database item; explain if included in a stored total."
            elif role in ("subtotal", "total", "before_tax"):
                # Page-scoped item evidence is necessary for multi-order PDFs.
                expected = Decimal(0)
                accounted = True
                matched_items = set()
                for candidate in page_prices:
                    if candidate["role"] not in ("item", "insurance", "discount", "tariff"): continue
                    match = _item_correspondence(receipt, candidate)
                    if match:
                        key, index, item, field = match
                        if (key, index) not in matched_items:
                            expected += item[0]
                            matched_items.add((key, index))
                    else:
                        accounted = False
                        likely = _item_candidates(receipt, candidate)
                        if len(likely) == 1:
                            key, index, item, _ = likely[0]
                            if (key, index) not in matched_items:
                                expected += item[0]
                                matched_items.add((key, index))
                total_pages = sum(any(p["role"] == "total" for p in row) for row in receipt["verification"])
                if role in ("total", "before_tax") and total_pages <= 1:
                    # A receipt spanning pages can be stored as one total-only
                    # database item. Component mismatches remain separate.
                    expected = receipt["allocated_total"]
                    accounted = True
                    if role == "total": expected += receipt["excluded_tax"]
                elif role != "subtotal":
                    detected = sum((v for _, kind, v in receipt["charges"] if kind == "shipping"), Decimal(0))
                    page_shipping = sum((v for ref, kind, v in receipt["charges"] if kind == "shipping"
                                         and ref.startswith(f'p{price["page"] + 1}.')), Decimal(0))
                    shipping = Decimal(str(receipt.get("shipping") or "0"))
                    if detected:
                        expected += (shipping * page_shipping / detected).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
                    if role == "total": expected += sum((p["value"] for p in page_prices if p["role"] == "tax"), Decimal(0))
                match = _item_correspondence(receipt, price)
                if match:
                    key, index, item, field = match
                    item_ref = (key, index)
                    status = "strike" if project is not None and key != project else "ok"
                    reason = f"{role.replace('_', ' ').capitalize()} matches {item[1]} in the database: ${value:,.2f}."
                elif accounted and expected == value:
                    status, reason = "ok", f"{role.replace('_', ' ').capitalize()} matches database purchases and entered invoice shipping: ${expected:,.2f}."
                else:
                    reason = (f"{role.replace('_', ' ').capitalize()}: listed in database as ${expected:,.2f}, "
                              f"but on receipt as ${value:,.2f}."
                              + (" Some receipt items could not be matched to database items." if not accounted else "")
                              + (" The receipt total does not balance with database purchases and entered invoice shipping."
                                 if receipt["unresolved"] != 0 else ""))
            else:
                match = _item_correspondence(receipt, price)
                if match:
                    key, index, item, field = match
                    status = "strike" if project is not None and key != project else "ok"
                    reason = f"{key[1]}, item {index + 1}, {field}: {item[2]} x {item[3]} = {item[0]}"
                    if status == "strike": reason += "; allocated to another project"
            if price["line"] is None:
                status, reason = "?", "Could not position this amount on the original page; correspondence requires manual review"
            elif price["confidence"] < 60 and not (role in ("total", "before_tax", "shipping") and status in ("ok", "strike")):
                status, reason = "?", "Low-confidence OCR; " + reason
            candidates = _item_candidates(receipt, price) if role == "item" else []
            if role == "item" and match:
                item_ref = (match[0], match[1])
            if role == "item" and not match and len(candidates) == 1:
                key, index, item, row_prices = candidates[0]
                item_ref = (key, index)
                expected = item[0] if price["value"] != item[3] else item[3]
                if price["value"] not in (item[0], item[3]):
                    # When a row prints both unit and extended prices, compare
                    # each with its corresponding database field.
                    field = "unit cost" if len(row_prices) > 1 and price["line"][0] == min(p["line"][0] for p in row_prices) else "item price"
                    expected = item[3] if field == "unit cost" else item[0]
                    reason = (f"{item[1]} ({field}): listed in database as ${expected:,.2f}, "
                              f"but on receipt as ${value:,.2f}.")
                else:
                    reason = (f"{item[1]}: the price ${value:,.2f} was found, but the quantity "
                              f"does not confirm the database quantity of {item[2]}.")
                if price["confidence"] < 60:
                    reason += " OCR is uncertain; check the receipt."
            if status == "?":
                reason = f"OCR amount ${value:,.2f} ({role.replace('_', ' ')}; {price['context']}): " + reason
                if receipt.get("explanation"):
                    reason += " See invoice explanation above."
            decisions.append(dict(price, status=status, reason=reason, item_ref=item_ref))
        result.append(decisions)
    # Re-search every original page after classification, comparing occurrences
    # (page/index/coordinates/value), so repeated equal numbers stay distinct.
    final = monetary_pages(receipt["pages"], receipt["text_geometry"])
    for matches, decisions in zip(final, result):
        identity = lambda p: (p["id"], p["line"], p["value"])
        previous = {identity(p): p for p in decisions}
        decisions[:] = [previous.get(identity(p), dict(
            p, status="?", reason="Final coverage check found an amount without a verification decision; manual review required"))
            for p in matches]
    return result


def _item_candidates(receipt, price):
    if price["line"] is None:
        return []
    cache = receipt.setdefault("item_candidates", {})
    identity = (price["page"], price["id"], price["line"], price["value"])
    if identity in cache:
        return cache[identity]
    spans = price["spans"]
    matches = []
    all_items = [(key, i, item) for key, items in receipt.get("source_groups", receipt["groups"]).items()
                 for i, item in enumerate(items)]
    selections = [_description_matches(spans, item[1]) for _, _, item in all_items]
    product_lines = set().union(*selections) if selections else set()
    prices = _prices(spans)
    position = next((i for i, p in enumerate(prices) if p["line"] == price["line"]), None)
    for (key, index, item), selected in zip(all_items, selections):
        if not selected or position not in _row_prices(spans, selected, product_lines, prices): continue
        row = [prices[j] for j in _row_prices(spans, selected, product_lines, prices)
               if amount_role(dict(prices[j], spans=spans, context="")) in ("item", "insurance", "discount", "tariff")]
        if not any(p["line"] == price["line"] for p in row):
            continue
        matches.append((key, index, item, row))
    cache[identity] = matches
    return matches


def _description_matches(spans, description):
    selected = _matches(spans, _anchors(description, []))
    if selected:
        return selected
    # Changed/reordered vendor titles can still supply a likely match. Require
    # several substantial words so generic product terms do not suffice.
    wanted = {t for t in _tokens(description) if len(t) >= 4}
    rows = {}
    for i, span in enumerate(spans):
        rows.setdefault(round(span["line"][1], 1), []).append(i)
    scores = []
    for owners in rows.values():
        words = {t for i in owners for t in _tokens(spans[i]["text"]) if len(t) >= 4}
        overlap = wanted & words
        if len(overlap) >= 3 and len(overlap) / min(len(wanted), len(words)) >= .6:
            selected = {i for i in owners if wanted & set(_tokens(spans[i]["text"]))}
            scores.append((len(overlap), selected))
    best = max((score for score, _ in scores), default=0)
    return set().union(*(owners for score, owners in scores if score == best))


def _item_correspondence(receipt, price):
    # A stored combined purchase can be supported by the printed total even
    # when its component descriptions differ from the database description.
    if price.get("role") in ("total", "before_tax"):
        exact = [(key, index, item, "receipt total")
                 for key, items in receipt.get("source_groups", receipt["groups"]).items()
                 for index, item in enumerate(items) if item[0] == price["value"]]
        if len(exact) == 1:
            return exact[0]
    matches = []
    for key, index, item, row in _item_candidates(receipt, price):
        amount, description, quantity, cost = item
        if (cost * quantity).quantize(Decimal('.01'), rounding=ROUND_HALF_UP) != amount: continue
        # If the source prints a quantity, it must agree with the purchase.
        row_text = price["context"]
        row_text += " " + " ".join(price["spans"][p["owner"]]["text"] for p in row)
        quantities = re.findall(r"(?:qty|quantity)\s*:?\s*(\d+)\b", row_text, re.I)
        if quantities and any(Decimal(q) != quantity for q in quantities): continue
        if quantity != 1 and not quantities:
            # A unit price alone cannot validate a multi-unit purchase. Require
            # its extension or an explicit source quantity in the product card.
            source_extensions = [p["value"] for p in row if p["value"] == amount]
            if not source_extensions: continue
        field = "unit cost" if price["value"] == cost else "extended price" if price["value"] == amount else None
        if field: matches.append((key, index, item, field))
    return matches[0] if len(matches) == 1 else None
