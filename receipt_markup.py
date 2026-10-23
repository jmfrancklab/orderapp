"""Locate excluded purchases in supporting PDFs without changing the source files."""
import csv
from decimal import Decimal
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
            if not word.get("text", "").strip() or float(word.get("conf", -1)) < 60:
                continue
            left, top, w, h = (int(word[key]) for key in ("left", "top", "width", "height"))
            y = float(box.top) - (top + h * .55) * scale_y
            spans.append({"text": word["text"], "height": h * scale_y, "line": (
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
    for length in (3, 4, 5, 6):
        for start in range(len(tokens) - length + 1):
            candidate = tuple(tokens[start:start + length])
            # Vendor titles often reorder adjectives. A distinctive three-word
            # phrase ("desk pad protector") can survive when longer phrases do
            # not; exclude short/generic connective and marketplace phrases.
            if length == 3 and any(len(word) < 3 or word in {
                    "amazon", "www", "com", "with", "for", "and", "the", "from"
            } for word in candidate):
                continue
            candidates.add(candidate)
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


_PRICE = re.compile(r"(?<![a-zA-Z0-9])(?:[$£€]\s*)?[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2,5}(?![a-zA-Z0-9])")


def _prices(spans):
    prices = []
    for index, span in enumerate(spans):
        text = span["text"]
        matches = list(_PRICE.finditer(text))
        remainder = _PRICE.sub("", text).strip().casefold()
        numeric_cell = not re.sub(r"(?:usd|price|amount|total|each|per|unit|tariff|[:$/()\s-])", "", remainder)
        width = stringWidth(text, "Helvetica", 1) or 1
        x0, y0, x1, y1 = span["line"]
        for match in matches:
            if not numeric_cell and not match.group().lstrip().startswith(("$", "£", "€")):
                continue
            start = stringWidth(text[:match.start()], "Helvetica", 1) / width
            end = stringWidth(text[:match.end()], "Helvetica", 1) / width
            value = Decimal(re.sub(r"[^0-9.+-]", "", match.group()))
            prices.append({"owner": index, "value": value, "line": (
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
