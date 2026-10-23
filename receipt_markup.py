"""Locate excluded purchases in supporting PDFs without changing the source files."""
import csv
from io import BytesIO, StringIO
import math
import re
import shutil
import subprocess
import unicodedata

from pypdf import PdfWriter
from reportlab.pdfbase.pdfmetrics import stringWidth


def _tokens(text):
    return re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKC", text).casefold())


def text_spans(page):
    """Text plus strike endpoints in unscaled PDF coordinates, using its matrices."""
    spans = []

    def visitor(text, cm, tm, font, size):
        if not text.strip():
            return
        x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
        y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
        dx, dy = (tm[0] * cm[0] + tm[1] * cm[2]) * size, (tm[0] * cm[1] + tm[1] * cm[3]) * size
        ux, uy = (tm[2] * cm[0] + tm[3] * cm[2]) * size, (tm[2] * cm[1] + tm[3] * cm[3]) * size
        if math.hypot(dx, dy) < .01 or math.hypot(ux, uy) < .01:
            return
        name = str((font or {}).get("/BaseFont", ""))
        family = "Helvetica-Bold" if "bold" in name.lower() else "Helvetica"
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
            spans.append({"text": line, "line": (start_x, start_y,
                          start_x + advance * dx, start_y + advance * dy)})

    try:
        page.extract_text(visitor_text=visitor)
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
            spans.append({"text": word["text"], "line": (
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


def strike_through(pages, excluded, relevant, geometry=None):
    """Return page-specific lines and unmatched descriptions; protect relevant items."""
    if geometry is None:
        geometry = [{"native": text_spans(page)} for page in pages]
    protected = [" ".join(_tokens(description)) for description in relevant]
    patterns = [_anchors(description, protected) for description in excluded]
    lines = [set() for _ in pages]
    found = set()
    for page_index, (page, data) in enumerate(zip(pages, geometry)):
        protected_lines = set()
        for description in relevant:
            protected_lines.update(_matches(data["native"], _anchors(description, [])))
        for item_index, anchors in enumerate(patterns):
            selected = _matches(data["native"], anchors) - protected_lines
            if selected:
                found.add(item_index)
                lines[page_index].update(data["native"][index]["line"] for index in selected)
        # OCR only a scanned/image page whose native text did not locate an item.
        if len(found) < len(excluded) and sum(len(span["text"]) for span in data["native"]) < 40:
            if "ocr" not in data:
                data["ocr"] = ocr_spans(page)
            protected_ocr = set()
            for description in relevant:
                protected_ocr.update(_matches(data["ocr"], _anchors(description, [])))
            for item_index, anchors in enumerate(patterns):
                selected = _matches(data["ocr"], anchors) - protected_ocr
                if selected:
                    found.add(item_index)
                    lines[page_index].update(data["ocr"][index]["line"] for index in selected)
    return [sorted(page_lines) for page_lines in lines], [
        description for index, description in enumerate(excluded) if index not in found]
