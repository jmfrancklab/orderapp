"""Locate excluded purchases in supporting PDFs without changing the source files."""
import csv
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO, StringIO
import math
import re
import shutil
import subprocess
import unicodedata

from pypdf import PdfWriter
from pypdf import _cmap
from reportlab.pdfbase.pdfmetrics import stringWidth


def _tokens(text):
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


def ocr_spans(page):
    """Use system Poppler/Tesseract via stdin/stdout; create no receipt files."""
    if not shutil.which("pdftoppm") or not shutil.which("tesseract"):
        return []
    writer = PdfWriter()
    writer.add_page(page)
    pdf = BytesIO()
    writer.write(pdf)
    try:
        image = subprocess.run(
            ["pdftoppm", "-singlefile", "-cropbox", "-scale-to", "2400", "-png", "-"],
            input=pdf.getvalue(), capture_output=True, check=True, timeout=30).stdout
        result = subprocess.run(
            ["tesseract", "stdin", "stdout", "--psm", "3", "tsv"],
            input=image, capture_output=True, check=True, timeout=45).stdout.decode("utf-8")
        from PIL import Image
        width, height = Image.open(BytesIO(image)).size
        box = page.cropbox
        scale_x, scale_y = float(box.width) / width, float(box.height) / height
        spans = []
        for word in csv.DictReader(StringIO(result), delimiter="\t"):
            if not word.get("text", "").strip():
                continue
            left, top, w, h = (int(word[key]) for key in ("left", "top", "width", "height"))
            y = float(box.top) - (top + h * .55) * scale_y
            spans.append({"text": word["text"], "confidence": float(word.get("conf", -1)), "height": h * scale_y, "line": (
                float(box.left) + left * scale_x, y,
                float(box.left) + (left + w) * scale_x, y)})
        return spans
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


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
    anchors = []
    for candidate in candidates:
        # A short device name such as TLV333 / AD7902 is commonly printed with
        # its package suffix (TLV333IDRCT / AD7902SDZ) on the vendor's receipt.
        suffix = r"[a-z0-9]*" if len(candidate) == 1 else ""
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


# Decimal amounts anywhere, plus explicitly currency-denominated whole amounts.
_WHOLE_AMOUNT_LABELS = ("shipping", "freight", "insurance", "discount", "tariff", "tax",
                        "subtotal", "total", "unit price", "unit cost", "price", "amount")
_PRICE = re.compile(
    r"(?<![a-zA-Z0-9.])(?:[+-]?\s*[$£€]\s*[+-]?\s*\d[\d,]*(?:\.\d{2,5})?"
    r"|[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2,5})(?![a-zA-Z0-9.])|(?:"
    + "|".join(r"(?<=" + re.escape(label + suffix) + ")"
               for label in _WHOLE_AMOUNT_LABELS for suffix in (" ", ": "))
    + r")[+-]?\d+(?![a-zA-Z0-9.])", re.I)



def _prices(spans):
    prices = []
    for index, span in enumerate(spans):
        text = span["text"]
        matches = list(_PRICE.finditer(text))
        width = stringWidth(text, "Helvetica", 1) or 1
        x0, y0, x1, y1 = span["line"]
        for match in matches:
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
        candidates = [(index, price) for index, price in enumerate(prices)
                      if price["line"][0] >= x - height
                      and lower < price["line"][1] <= y + height*.65
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
        geometry = [{"native": text_spans(page)} for page in pages]
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
        mark(data["native"], page_index)
        if len(found) < len(excluded) and sum(len(span["text"]) for span in data["native"]) < 40:
            if "ocr" not in data:
                data["ocr"] = ocr_spans(page)
            mark(data["ocr"], page_index)
    return [sorted(page_lines) for page_lines in lines], [
        description for index, description in enumerate(excluded) if index not in found]


def monetary_pages(pages, geometry):
    """Regex every page, retaining occurrence identity and source geometry."""
    from collections import Counter
    result = []
    for page_index, (page, data) in enumerate(zip(pages, geometry)):
        if "native" not in data:
            data["native"] = text_spans(page)
        spans = data["native"]
        if not spans and (not (page.extract_text() or "").strip() or _PRICE.search(page.extract_text() or "")):
            if "ocr" not in data:
                data["ocr"] = ocr_spans(page)
            spans = data["ocr"]
            if not spans:
                raise ValueError(f"Cannot verify supporting page {page_index + 1}: no readable text or OCR.")
        # This second search covers the complete original extraction, including
        # strings the geometry visitor may have been unable to position.
        raw = page.extract_text() or ""
        raw_values = Counter(Decimal(re.sub(r"[^0-9.+-]", "", m.group()))
                             for m in _PRICE.finditer(raw))
        prices = _prices(spans)
        positioned = Counter(p["value"] for p in prices)
        if raw_values - positioned:
            raise ValueError(f"Cannot position every monetary amount on supporting page {page_index + 1}.")
        for index, price in enumerate(prices):
            owner = price["owner"]
            span = spans[owner]
            x, y = price["line"][:2]
            height = span.get("height", 10)
            row = [s["text"] for s in spans if abs(s["line"][1] - y) <= height * .65]
            context = " ".join(row)
            # OCR produces individual words; row geometry reunites labels.
            price.update(id=f"P{page_index + 1}.{index + 1}", page=page_index,
                         context=context, spans=spans)
        result.append(prices)
    return result


_SUMMARY_LABELS = (
    ("tax", r"(?:sales\s+tax|estimated\s+tax|\btax\b)"),
    ("discount", r"(?:discount|coupon|savings|promotion)"),
    ("insurance", r"insurance"),
    ("shipping", r"(?:shipping|freight|delivery charge)"),
    ("tariff", r"(?:tariff|duty|surcharge|handling fee|other charge)"),
    ("subtotal", r"(?:subtotal|sub total|merchandise|item\(s\) subtotal)"),
    ("before_tax", r"total before tax"),
    ("total", r"(?:grand total|total billed|order total|amount paid|(?<!sub )\btotal\b)"),
)


def amount_role(price):
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
    matches = [(m.start(), -rank, role) for rank, (role, pattern) in enumerate(_SUMMARY_LABELS)
               for m in re.finditer(pattern, text)]
    return max(matches)[2] if matches else "item"


def reconcile(receipt):
    """Keep purchase tuples intact; store source evidence and allocations alongside."""
    cent = Decimal(".01")
    geometry = receipt.setdefault("text_geometry", [{} for _ in receipt["pages"]])
    evidence = monetary_pages(receipt["pages"], geometry)
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
            # A charge row may print both unit and extended amounts. Allocate
            # only the rightmost extension, even when the unit amount differs.
            price = max(row, key=lambda p: p["line"][0])
            value = -abs(price["value"]) if role == "discount" else price["value"]
            if role == "tax":
                tax += value
            else:
                charges.append((price["id"], role, value))
    # One terminal total per page; multi-order supporting files sum their orders.
    total = sum((next((p["value"] for p in reversed(row) if p["role"] == "total"), Decimal(0))
                 for row in evidence), Decimal(0)) if totals else None
    allocations = {(key, i): {"shipping": Decimal(0), "other": Decimal(0)} for key, i, _ in items}
    for _, role, value in charges:
        if not items or base <= 0:
            continue
        # Cumulative rounding conserves every cent and is deterministic.
        previous, cumulative = Decimal(0), Decimal(0)
        for key, i, item in items:
            cumulative += item[0]
            share = (value * cumulative / base).quantize(cent, rounding=ROUND_HALF_UP)
            allocations[key, i]["shipping" if role == "shipping" else "other"] += share - previous
            previous = share
    reimbursable = base + sum((v for _, _, v in charges), Decimal(0))
    residual = total - tax - reimbursable if total is not None else None
    receipt.update(verification=evidence, charges=charges, excluded_tax=tax,
                   original_total=total, reimbursable_total=total - tax if total is not None else None,
                   allocated_total=reimbursable, unresolved=residual, allocations=allocations)
    return receipt


def verify_amounts(receipt, project=None):
    """One decision per occurrence; equal numeric values alone never verify items."""
    result = []
    charge_refs = {role: [(ref, value) for ref, kind, value in receipt["charges"] if kind == role]
                   for role in ("shipping", "insurance", "discount", "tariff")}
    for page_prices in receipt["verification"]:
        decisions = []
        for price in page_prices:
            role, value = price["role"], price["value"]
            status, reason = "?", "No contextual correspondence to a purchase or calculation"
            if role == "tax":
                status, reason = "strike", "Excluded sales tax; included only in original receipt total"
            elif role in charge_refs:
                row_refs = [(ref, value) for ref, value in charge_refs[role]
                            if ref.startswith(f'P{price["page"] + 1}.')]
                if any(abs(value) == abs(price["value"]) for _, value in row_refs):
                    references = "; ".join(f"{ref} = {value:,.2f}" for ref, value in row_refs)
                    status, reason = "ok", f"Receipt adjustment {role}: {references}"
                else:
                    references = "; ".join(f"{ref} = {value:,.2f}" for ref, value in row_refs)
                    reason = f"Adjustment unit price needs quantity verification against {references}"
            elif role in ("subtotal", "total", "before_tax"):
                # Page-scoped item evidence is necessary for multi-order PDFs.
                expected = Decimal(0)
                accounted = True
                matched_items = set()
                for candidate in page_prices:
                    if candidate["role"] != "item": continue
                    match = _item_correspondence(receipt, candidate)
                    if match:
                        key, index, item, field = match
                        if (key, index) not in matched_items:
                            expected += item[0]
                            matched_items.add((key, index))
                    else: accounted = False
                page_charges = {kind: sum((v for ref, kind2, v in receipt["charges"]
                                          if kind2 == kind and ref.startswith(f'P{price["page"] + 1}.')), Decimal(0))
                                for kind in charge_refs}
                if role != "subtotal":
                    expected += sum(page_charges.values(), Decimal(0))
                else:
                    expected += page_charges.get("insurance", Decimal(0))
                if role == "total": expected += sum((p["value"] for p in page_prices if p["role"] == "tax"), Decimal(0))
                if matched_items and accounted and expected == value and receipt["unresolved"] == 0:
                    status, reason = "ok", f"Page {price['page'] + 1} {role}: item extensions plus applicable adjustments = {expected}"
                else:
                    reason = f"Page {price['page'] + 1} {role}: receipt {value}; contextually verified calculation {expected}; unresolved item evidence={not accounted}"
            else:
                match = _item_correspondence(receipt, price)
                if match:
                    key, index, item, field = match
                    status = "strike" if project is not None and key != project else "ok"
                    reason = f"{key[1]}, item {index + 1}, {field}: {item[2]} x {item[3]} = {item[0]}"
                    if status == "strike": reason += "; allocated to another project"
            if price["confidence"] < 60:
                status, reason = "?", "Low-confidence OCR; " + reason
            decisions.append(dict(price, status=status, reason=reason))
        result.append(decisions)
    # Re-search every original page after classification, comparing occurrences
    # (page/index/coordinates/value), so repeated equal numbers stay distinct.
    final = monetary_pages(receipt["pages"], receipt["text_geometry"])
    for matches, decisions in zip(final, result):
        identity = lambda p: (p["id"], p["line"], p["value"])
        if ([identity(p) for p in matches] != [identity(p) for p in decisions]
                or len({p["id"] for p in decisions}) != len(matches)):
            raise ValueError("Incomplete monetary annotation coverage")
    return result


def _item_correspondence(receipt, price):
    spans = price["spans"]
    matches = []
    all_items = [(key, i, item) for key, items in receipt["groups"].items() for i, item in enumerate(items)]
    selections = [_matches(spans, _anchors(item[1], [])) for _, _, item in all_items]
    product_lines = set().union(*selections) if selections else set()
    prices = _prices(spans)
    position = next((i for i, p in enumerate(prices) if p["line"] == price["line"]), None)
    for (key, index, item), selected in zip(all_items, selections):
        if not selected or position not in _row_prices(spans, selected, product_lines, prices): continue
        amount, description, quantity, cost = item
        if (cost * quantity).quantize(Decimal('.01'), rounding=ROUND_HALF_UP) != amount: continue
        # If the source prints a quantity, it must agree with the purchase.
        row_text = price["context"]
        row_indices = _row_prices(spans, selected, product_lines, prices)
        row_text += " " + " ".join(spans[prices[j]["owner"]]["text"] for j in row_indices)
        quantities = re.findall(r"(?:qty|quantity)\s*:?\s*(\d+)\b", row_text, re.I)
        if quantities and any(Decimal(q) != quantity for q in quantities): continue
        if quantity != 1 and not quantities:
            # A unit price alone cannot validate a multi-unit purchase. Require
            # its extension or an explicit source quantity in the product card.
            source_extensions = [prices[j]["value"] for j in row_indices if prices[j]["value"] == amount]
            if not source_extensions: continue
        field = "unit cost" if price["value"] == cost else "extended price" if price["value"] == amount else None
        if field: matches.append((key, index, item, field))
    return matches[0] if len(matches) == 1 else None
