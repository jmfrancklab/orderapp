"""Filtered purchase summaries with reviewed costs and bitmap supporting pages."""
from collections import OrderedDict
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from io import BytesIO
import ipaddress
from pathlib import Path
import re
import socket
from time import monotonic
from urllib.parse import urljoin, urlparse
from xml.sax.saxutils import escape

import requests
from PIL import Image as PILImage
from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    Flowable, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer,
    Table, TableStyle,
)

import quotes
import receipt_markup

PAGE_WIDTH, PAGE_HEIGHT = letter
MARGIN = 36
CONTENT_WIDTH = PAGE_WIDTH - 2 * MARGIN
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_DOCUMENT_PAGES = 100
CENT = Decimal("0.01")
OTHER_PROJECT_COLOR = colors.HexColor("#b00020")
pdfmetrics.registerFont(TTFont("ReceiptSymbols", str(
    Path(__file__).with_name("report_fonts") / "ReceiptSymbols.ttf")))

_MONTH = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
_DATE_PATTERNS = (
    ("iso", re.compile(r"(?<!\d)(20\d{2}|19\d{2})\s*[-/]\s*(\d{1,2})\s*[-/]\s*(\d{1,2})(?!\d)")),
    ("numeric", re.compile(r"(?<![\d/])((?:0[ \t]*\n[ \t]*)?\d{1,2})\s*[/\-]\s*(\d{1,2})\s*[/\-]\s*((?:20|19)\d{2}|\d{2})(?!\d)")),
    ("month_first", re.compile(rf"\b({_MONTH})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\s*,?\s*(20\d{{2}}|19\d{{2}})(?!\d)", re.I)),
    ("day_first", re.compile(rf"(?<!\d)(\d{{1,2}})\s*[- ]\s*({_MONTH})\.?\s*[- ,]\s*(20\d{{2}}|19\d{{2}})", re.I)),
)
_MONTH_NUMBERS = {name: index for index, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def _date_label(label):
    # PDF extraction can split labels into fragments such as "Orde r Date".
    return r"\b" + r"\s*".join(re.escape(char) for char in label if char != " ")


_DATE_LABELS = [
    (100, "transaction date|payment date|date paid|paid on|purchase date|purchased on"),
    (90, "order date|order placed|placed on|placed|submitted on|submitted"),
    (80, "invoice date|receipt date"),
    (60, "date"),
]
_DATE_LABEL_PATTERNS = [
    (priority, re.compile(
        "(?:" + "|".join(_date_label(label) for label in labels.split("|")) + ")"
        r"\b[\s\S]*$", re.I))
    for priority, labels in _DATE_LABELS
]
_NON_TRANSACTION_LABEL = re.compile(
    "(?:" + "|".join(_date_label(label) for label in (
        "eligible through", "return by", "return until", "arriving", "arrival date",
        "delivery date", "delivered", "ship date", "shipping date", "document date",
        "due date", "expires", "expiration", "expiration date")) + r")\s*:?\s*$", re.I)


def transaction_date(pages, geometry=None):
    """Prefer date-label regex matches; fall back to a numeric date anywhere."""
    candidates = []
    numeric_fallback = []
    for page_index, page in enumerate(pages or []):
        data = geometry[page_index] if geometry is not None else {}
        text = "\n".join(span["text"] for span in receipt_markup.receipt_spans(page, data))
        # Match dates in the original text: joining numeric lines can merge an
        # order/reference number into the following date and hide valid matches.
        for kind, pattern in _DATE_PATTERNS:
            for match in pattern.finditer(text):
                a, b, c = (re.sub(r"\s+", "", value) for value in match.groups())
                if kind == "iso":
                    year, month, day = int(a), int(b), int(c)
                else:
                    year = int(c)
                    if year < 100:
                        year += 2000 if year < 70 else 1900
                    if kind == "numeric":
                        month, day = int(a), int(b)
                    elif kind == "month_first":
                        month, day = _MONTH_NUMBERS[a[:3].lower()], int(b)
                    else:
                        month, day = _MONTH_NUMBERS[b[:3].lower()], int(a)
                try:
                    value = date(year, month, day)
                except ValueError:
                    continue
                if kind in ("numeric", "iso"):
                    numeric_fallback.append((page_index, match.start(), value))
                before = text[:match.start()]
                if _NON_TRANSACTION_LABEL.search(before):
                    continue
                priority = max((score for score, label in _DATE_LABEL_PATTERNS
                                if label.search(before)), default=0)
                candidates.append((priority, page_index, match.start(), value))
    if not candidates:
        return min(numeric_fallback)[2] if numeric_fallback else None
    # Multiple dates no longer suppress an otherwise usable regex match.
    return min(candidates, key=lambda item: (-item[0], item[1], item[2]))[3]


class ReportError(Exception):
    def __init__(self, errors):
        self.errors = errors
        super().__init__("\n\n".join(error["message"] for error in errors))


def _public_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Use a public HTTP or HTTPS file link.")
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("The file link must point to a public internet address.")


def download_document(url):
    """Download public files with provider rewrites, bounded size and redirects."""
    provider = quotes.classify_link(url)
    if provider in quotes._DIRECT:
        url = quotes._DIRECT[provider](url)
    # Anonymous SharePoint links set FedAuth before redirecting to the PDF.
    # Keep that cookie for this document's redirect chain, like a browser does.
    with requests.Session() as session:
        for _ in range(6):
            _public_url(url)
            with session.get(url, stream=True, timeout=(5, 20), allow_redirects=False,
                             headers={"User-Agent": "ACERT-ordering/1.0"}) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers["Location"])
                    continue
                response.raise_for_status()
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > MAX_FILE_BYTES:
                        raise ValueError("Supporting document exceeds 25 MB.")
                    chunks.append(chunk)
                return b"".join(chunks)
    raise ValueError("Too many redirects; provide a direct file or public share link.")


def document_pages(data):
    if not data.startswith(b"%PDF"):
        # ReportLab embeds the original image pixels; no OCR is required.
        with PILImage.open(BytesIO(data)) as image:
            if image.format not in ("PNG", "JPEG"):
                raise ValueError("Supporting files must be PDF, PNG, or JPEG.")
            image.load()
            buf = BytesIO()
            width, height = image.size
            pdf = canvas.Canvas(buf, pagesize=(width, height))
            pdf.drawImage(ImageReader(image), 0, 0, width, height, mask="auto")
            pdf.save()
            data = buf.getvalue()
    reader = PdfReader(BytesIO(data))
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("The supporting PDF is password protected.")
    if not 0 < len(reader.pages) <= MAX_DOCUMENT_PAGES:
        raise ValueError("Supporting documents must contain 1–100 pages.")
    pages = []
    for original in reader.pages:
        page = deepcopy(original)
        page.transfer_rotation_to_content()
        if float(page.cropbox.width) <= 0 or float(page.cropbox.height) <= 0:
            raise ValueError("The supporting PDF has an invalid page size.")
        # Force content parsing while errors can still be tied to this invoice.
        page.get_contents()
        pages.append(page)
    return pages


def _error(invoice, reason, document=True):
    action = ("Filter to rows with an accessible receipt or invoice, or provide a receipt for this row."
              if document else "Correct this purchase or filter out this invoice.")
    error = {"invoice_id": invoice["id"], "nickname": invoice["nickname"],
             "kind": "document" if document else "purchase",
             "message": f'Receipt #{invoice["id"]} ({invoice["nickname"]}): {reason} {action}'}
    if document:
        field = "receipt_url" if (invoice["receipt_url"] or "").strip() else "invoice_url"
        if not (invoice[field] or "").strip():
            field = "receipt_url"
        error.update(source_field=field, source_url=invoice[field] or "")
    return error


class ReportProgress:
    """Estimate remaining work from stage counts and observed seconds per unit.

    Page totals start as estimates and become exact after downloads. Nothing
    advances on a timer: each snapshot follows real work or a stage transition.
    """
    DEFAULTS = {"purchases": .01, "downloads": 1., "ocr": 3.,
                "reconcile": .05, "matching": .1, "sections": .05,
                "layout": .5, "merge": .1, "write": .3}
    LABELS = {"purchases": "Loading invoice purchase data", "downloads": "Downloading documents",
              "ocr": "Reading document pages", "reconcile": "Reconciling invoices",
              "matching": "Matching purchase sections", "sections": "Building purchase sections",
              "layout": "Laying out PDF and bookmarks", "merge": "Preparing supporting images",
              "write": "Writing PDF"}

    def __init__(self, invoices):
        count = len(invoices)
        self.totals = dict(purchases=count, downloads=count, ocr=count * 3,
                           reconcile=count, matching=count, sections=count,
                           layout=1, merge=count * 3, write=1)
        self.done = dict.fromkeys(self.totals, 0)
        self.rates = dict(self.DEFAULTS)
        self.started = monotonic()

    def start(self, stage, total=None):
        self.stage = stage
        if total is not None:
            self.totals[stage] = total
        self.stage_started = monotonic()
        return self.snapshot()

    def advance(self):
        self.done[self.stage] += 1
        elapsed = monotonic() - self.stage_started
        self.rates[self.stage] = elapsed / self.done[self.stage]
        return self.snapshot()

    def snapshot(self):
        remaining = sum(max(0, self.totals[key] - self.done[key]) * self.rates[key]
                        for key in self.totals)
        elapsed = monotonic() - self.started
        return dict(type="progress", stage=self.stage, label=self.LABELS[self.stage],
                    completed=self.done[self.stage], total=self.totals[self.stage],
                    elapsed_seconds=round(elapsed, 2), remaining_seconds=round(remaining, 1),
                    percent=round(min(99, 100 * elapsed / max(.001, elapsed + remaining)), 1))


def _consume(events):
    """Synchronous callers use the same pipeline, without progress transport."""
    while True:
        try:
            next(events)
        except StopIteration as result:
            return result.value


def collect_receipts(db, invoices):
    return _consume(_collect_receipts(db, invoices, ReportProgress(invoices)))


def _collect_receipts(db, invoices, progress):
    receipts, errors, documents = [], [], {}
    yield progress.start("purchases")
    for invoice in invoices:
        rows = db.execute(
            """SELECT o.id, o.description, o.use_note, o.cost, o.quantity, o.project_id, v.name AS vendor_name,
                      COALESCE(p.name, 'Unassigned project') AS project_name
               FROM orders o LEFT JOIN projects p ON p.id = o.project_id
               LEFT JOIN vendors v ON v.id = o.vendor_id
               WHERE o.invoice_id = ? ORDER BY o.id""", (invoice["id"],)).fetchall()
        groups = OrderedDict()
        use_notes = {}
        for row in rows:
            try:
                cost = Decimal(str(row["cost"]).replace(",", "").strip())
                if not cost.is_finite():
                    raise InvalidOperation
                amount = (cost * row["quantity"]).quantize(CENT, rounding=ROUND_HALF_UP)
            except (InvalidOperation, TypeError, ValueError):
                errors.append(_error(invoice, f'Order #{row["id"]} has a missing or invalid cost.', False))
                continue
            key = (row["project_id"], row["project_name"])
            use_notes[key, len(groups.get(key, []))] = row["use_note"]
            groups.setdefault(key, []).append((amount, row["description"], row["quantity"], cost))
        url = (invoice["receipt_url"] or "").strip() or (invoice["invoice_url"] or "").strip()
        documents.setdefault(url, {})
        vendors = list(dict.fromkeys(row["vendor_name"] for row in rows if row["vendor_name"]))
        receipts.append({"id": invoice["id"], "nickname": invoice["nickname"], "groups": groups,
                         "vendor": ", ".join(vendors) or "Vendor not recorded",
                         "provided_by": invoice["created_by"], "explanation": invoice["explanation"],
                         "shipping": Decimal(invoice["shipping"] or "0"), "use_notes": use_notes,
                         "source_url": url, "invoice": invoice})
        yield progress.advance()

    sections = sum(max(1, len(receipt["groups"])) for receipt in receipts)
    row_count = sum(len(items) for receipt in receipts for items in receipt["groups"].values())
    progress.rates["layout"] = .5 + .02 * row_count + .05 * sections
    progress.totals.update(matching=sections, sections=sections,
                           downloads=len(documents), ocr=3 * len(documents))
    yield progress.start("downloads")
    for url, document in documents.items():
        try:
            if not url:
                raise ValueError("No receipt or invoice URL is recorded.")
            pages = document_pages(download_document(url))
            document.update(pages=pages, geometry=[{} for _ in pages])
        except Exception as exc:
            # Never expose share URLs/tokens or remote exception bodies.
            document["failure"] = str(exc) if isinstance(exc, ValueError) else "Could not download or read the supporting file. Check its sharing permissions and file format."
        yield progress.advance()

    page_count = sum(len(doc.get("pages", [])) for doc in documents.values())
    embedded_pages = sum(len(documents[r["source_url"]].get("pages", []))
                         * max(1, len(r["groups"])) for r in receipts)
    progress.totals["merge"] = page_count
    progress.rates["layout"] += .03 * embedded_pages
    progress.rates["write"] = .3 + .02 * embedded_pages
    yield progress.start("ocr", page_count)
    for document in documents.values():
        for page, geometry in zip(document.get("pages", []), document.get("geometry", [])):
            receipt_markup.receipt_spans(page, geometry)
            yield progress.advance()
        document["transaction_date"] = transaction_date(document.get("pages"), document.get("geometry"))

    yield progress.start("reconcile")
    for receipt in receipts:
        document = documents[receipt.pop("source_url")]
        invoice = receipt.pop("invoice")
        if "failure" in document:
            errors.append(_error(invoice, document["failure"]))
        else:
            receipt.update(pages=document["pages"], text_geometry=document["geometry"],
                           transaction_date=document["transaction_date"])
            receipt_markup.reconcile(receipt)
        yield progress.advance()
    if errors:
        raise ReportError(errors)
    return receipts


class SupportingPage(Flowable):
    """Draw the OCR bitmap and annotations; never merge source PDF vectors."""
    def __init__(self, page, image, strikes=(), annotations=()):
        super().__init__()
        self.page, self.image = page, image
        self.strikes = strikes
        self.annotations = annotations
        width, height = float(page.cropbox.width), float(page.cropbox.height)
        self.scale = min(1, (CONTENT_WIDTH / 2 - 12) / width, 600 / height)
        self.width, self.height = width * self.scale, height * self.scale

    def draw(self):
        # Keep only compressed bytes between draws. A cached ImageReader would
        # retain a decoded RGB buffer for every high-resolution page at once.
        self.canv.drawImage(ImageReader(BytesIO(self.image)), 0, 0, self.width, self.height)
        if self.strikes:
            self.canv.saveState()
            clip = self.canv.beginPath()
            clip.rect(0, 0, self.width, self.height)
            self.canv.clipPath(clip, stroke=0)
            self.canv.setStrokeColor(OTHER_PROJECT_COLOR)
            self.canv.setStrokeAlpha(.5)
            self.canv.setLineWidth(.8)
            left, bottom = float(self.page.cropbox.left), float(self.page.cropbox.bottom)
            for x0, y0, x1, y1 in self.strikes:
                self.canv.line((x0 - left) * self.scale, (y0 - bottom) * self.scale,
                               (x1 - left) * self.scale, (y1 - bottom) * self.scale)
                # Description strikes share the nearby excluded amount's ID.
                nearby = [a for a in self.annotations if a["line"] is not None
                          and a["status"] == "strike" and a.get("item_ref") is not None]
                if nearby:
                    ref = min(nearby, key=lambda a: abs(a["line"][1] - y0))
                    self.canv.setFillColor(colors.HexColor("#008000"))
                    self.canv.setFillAlpha(.5)
                    self.draw_reference((x1-left)*self.scale + 2,
                                        (y1-bottom)*self.scale - 2, ref["id"], True)
            self.canv.restoreState()
        self.canv.saveState()
        left, bottom = float(self.page.cropbox.left), float(self.page.cropbox.bottom)
        self.canv.setFillAlpha(.5)
        self.canv.setStrokeAlpha(.5)
        for annotation in self.annotations:
            if annotation["line"] is None:
                continue
            x0, y0, x1, y1 = annotation["line"]
            x0, y0, x1, y1 = ((x0-left)*self.scale, (y0-bottom)*self.scale,
                              (x1-left)*self.scale, (y1-bottom)*self.scale)
            successful = annotation["status"] in ("ok", "strike")
            self.canv.setFillColor(colors.HexColor("#008000") if successful else OTHER_PROJECT_COLOR)
            self.canv.setFillAlpha(.5)
            # A single baseline: reference immediately precedes its Unicode mark.
            self.draw_reference(x1 + 2, y1 - 2, annotation["id"], successful)
            if annotation["status"] == "strike":
                self.canv.setStrokeColor(OTHER_PROJECT_COLOR)
                self.canv.setStrokeAlpha(.5)
                self.canv.setLineWidth(.8)
                self.canv.line(x0, y0, x1, y1)
        self.canv.restoreState()
        self.canv.setStrokeColor(colors.black)
        self.canv.setLineWidth(1)
        self.canv.rect(0, 0, self.width, self.height)

    def draw_reference(self, x, y, reference, successful):
        symbol = "✓" if successful else "❓"
        width = self.canv.stringWidth(reference + " ", "Helvetica", 5)
        total_width = width + self.canv.stringWidth(symbol, "ReceiptSymbols", 5)
        x = min(x, self.width - total_width)
        self.canv.setFont("Helvetica", 5)
        self.canv.drawString(x, y, reference + " ")
        self.canv.setFont("ReceiptSymbols", 5)
        self.canv.drawString(x + width, y, symbol)


SUBSECTION_GRAY = colors.HexColor("#b8b8b8")
TABLE_HEADER_GRAY = colors.HexColor("#ededed")


class ReportHeading(Flowable):
    """Full-width heading bars, preserving the paragraph's text and PDF outline."""
    def __init__(self, text, style, level):
        super().__init__()
        self.outline_level = level
        self.paragraph = Paragraph(text, ParagraphStyle(
            "BarHeading", parent=style, textColor=colors.white if level == 0 else colors.black))
        self.keepWithNext = True
        self.spaceBefore = style.spaceBefore
        self.spaceAfter = style.spaceAfter

    def getPlainText(self):
        return self.paragraph.getPlainText()

    def wrap(self, available_width, available_height):
        self.width = CONTENT_WIDTH
        _, self.text_height = self.paragraph.wrap(self.width - 16, available_height)
        self.height = self.text_height + 16 + (6 if self.outline_level == 0 else 0)
        return self.width, self.height

    def draw(self):
        c = self.canv
        c.saveState()
        top_level = self.outline_level == 0
        c.setFillColor(colors.black if top_level else SUBSECTION_GRAY)
        c.rect(0, 3 if top_level else 0, self.width,
               self.height - (6 if top_level else 0), fill=1, stroke=0)
        c.setStrokeColor(colors.black)
        c.setLineWidth(1)
        # Each 1pt rule ends exactly 2pt from the black bar.
        c.line(0, .5, self.width, .5)
        c.line(0, self.height - .5, self.width, self.height - .5)
        self.paragraph.drawOn(c, 8, 11 if top_level else 8)
        c.restoreState()


class NavigableReport(SimpleDocTemplate):
    """Register headings after layout, so bookmarks follow actual page breaks."""

    def afterFlowable(self, flowable):
        level = getattr(flowable, "outline_level", None)
        if level is None:
            return
        key = f"heading-{self.seq.nextf('outline')}"
        self.canv.bookmarkHorizontalAbsolute(key, self.frame._y + flowable.height)
        self.canv.addOutlineEntry(flowable.getPlainText(), key, level=level, closed=False)


def build_pdf(receipts, grouping):
    progress = ReportProgress(receipts)
    return _consume(_build_pdf(receipts, grouping, progress))


def _build_pdf(receipts, grouping, progress):
    # Work on report data only; original supporting pages and database stay intact.
    receipts = [dict(receipt) if "verification" in receipt else receipt_markup.reconcile(dict(receipt))
                for receipt in sorted(receipts, key=lambda receipt: receipt["id"])]
    # Compute decisions once per receipt/project, shared by tables and annotations.
    decisions = {}
    section_count = sum(max(1, len(receipt["groups"])) for receipt in receipts)
    yield progress.start("matching", section_count)
    for receipt in receipts:
        for project in list(receipt["groups"]) or [None]:
            decisions[id(receipt), project] = receipt_markup.verify_amounts(receipt, project)
            yield progress.advance()

    # Cache only compressed rasters. ReportLab reuses their image resources
    # across project sections; decoded page buffers live for one draw only.
    geometry_pages = {}
    for receipt in receipts:
        for page, data in zip(receipt["pages"], receipt["text_geometry"]):
            geometry_pages.setdefault(id(data), (page, data))
    yield progress.start("merge", len(geometry_pages))
    for page, data in geometry_pages.values():
        if "image_jpeg" not in data:
            data["image_jpeg"] = receipt_markup.page_jpeg(page)
        yield progress.advance()

    def item_total(receipt, key, index, item):
        return item[0] + sum(receipt["allocations"][key, index].values(), Decimal(0))

    def group_total(receipt, key):
        return sum((item_total(receipt, key, index, item)
                    for index, item in enumerate(receipt["groups"][key])), Decimal(0))

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle("OtherProject", parent=styles["Normal"], textColor=OTHER_PROJECT_COLOR))
    styles.add(ParagraphStyle("OtherProjectHeading", parent=styles["Heading3"], textColor=OTHER_PROJECT_COLOR))
    story = []

    def paragraph(text, style="Normal", outline_level=None):
        text = escape(str(text)).replace("\n", "<br/>")
        if outline_level in (0, 1):
            return ReportHeading(text, styles[style], outline_level)
        result = Paragraph(text, styles[style])
        result.outline_level = outline_level
        return result

    def receipt_label(receipt, short=False):
        label = f'{"" if short else "Receipt "}#{receipt["id"]}'
        if receipt.get("nickname"):
            total = receipt["allocated_total"]
            label += f' <font size="9">({escape(receipt["nickname"])}: ${total:,.2f})</font>'
        return label

    def receipt_title(receipt, prefix="", style="Heading2", outline_level=None, project=None):
        purchased_on = receipt.get("transaction_date")
        formatted_date = (f" {purchased_on.month}/{purchased_on.day}/{purchased_on.year % 100:02d}"
                          if purchased_on else "")
        text = (escape(prefix) + receipt_label(receipt) + ": "
                + escape(receipt.get("vendor", "Vendor not recorded")) + formatted_date)
        if project is not None:
            text += " — Relevant to project: " + escape(project[1])
        if outline_level in (0, 1):
            return ReportHeading(text, styles[style], outline_level)
        result = Paragraph(text, styles[style])
        result.outline_level = outline_level
        return result

    def purchases(items, project_names=None, allocations=None, locations=None, notes=None):
        other = project_names is not None
        style = "OtherProject" if other else "Normal"
        rows = [["Description"] + (["Project"] if other else []) + ["Location", "Quantity", "Unit cost", "Line total"]]
        for index, (amount, description, quantity, cost) in enumerate(items):
            description_text = escape(description).replace("\n", "<br/>")
            if notes and notes[index]:
                description_text += '<br/><font color="#808080"><i>' + escape(notes[index]).replace("\n", "<br/>") + '</i></font>'
            rows.append([Paragraph(description_text, styles[style])]
                        + ([paragraph(project_names[index], style)] if other else [])
                        + [locations[index] if locations else paragraph("Not found", "OtherProject"), str(quantity), f"{cost:,.5f}".rstrip("0").rstrip(".") if cost.as_tuple().exponent < -2 else f"{cost:,.2f}",
                           f"{amount:,.2f}"])
        total = sum((item[0] + sum(allocations[index].values(), Decimal(0))
                     for index, item in enumerate(items)), Decimal(0))
        rows.append(["OTHER PROJECTS TOTAL:" if other else "TOTAL:"]
                    + [""] * (len(rows[0]) - 2) + [f"{total:,.2f}"])
        widths = [205, 75, 70, 45, 65, 80] if other else [280, 70, 45, 65, 80]
        table = Table(rows, colWidths=widths,
                      hAlign="LEFT", repeatRows=1, splitInRow=1)
        table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ALIGN", (3 if other else 2, 0), (-1, -1), "RIGHT"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("BACKGROUND", (0, 0), (-1, 0), TABLE_HEADER_GRAY),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("LINEBELOW", (0, 0), (-1, 0), 0.7, colors.black),
            ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#f7f8fa")]),
            ("LINEABOVE", (0, -1), (-1, -1), 0.7, colors.black),
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ("SPAN", (0, -1), (-2, -1)),
            ("TEXTCOLOR", (0, 0), (-1, -1), OTHER_PROJECT_COLOR if other else colors.black),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ]))
        story.extend([table, Spacer(1, 12)])

    def documents(receipt, excluded=(), relevant=(), costs=None, project=None):
        pages = receipt["pages"]
        strikes, unmatched = [[] for _ in pages], []
        if excluded:
            if "text_geometry" not in receipt:
                receipt["text_geometry"] = [{} for _ in pages]
            strikes, unmatched = receipt_markup.strike_through(
                pages, excluded, relevant, receipt["text_geometry"], costs=costs)
        annotations = decisions[id(receipt), project]
        # Legacy description strikes remain, but monetary decisions have a single owner.
        strikes = [[line for line in lines if not any(
                mark["line"] is not None and abs(line[1] - mark["line"][1]) < 2 and
                line[0] < mark["line"][2] and line[2] > mark["line"][0] for mark in marks)] for lines, marks in zip(strikes, annotations)]
        heading = [receipt_title(receipt, "Supporting document — ", "Heading3",
                                 outline_level=2 if receipt["groups"] else 1, project=project),
                   paragraph(f'provided by: {receipt.get("provided_by", "not recorded")}'), Spacer(1, 6)]
        for index in range(0, len(pages), 2):
            cells = [SupportingPage(pages[page_index], receipt["text_geometry"][page_index]["image_jpeg"],
                                     strikes[page_index], annotations[page_index])
                     for page_index in range(index, min(index + 2, len(pages)))]
            if len(cells) == 1:
                cells.append("")
            table = Table([cells], colWidths=[CONTENT_WIDTH / 2] * 2, hAlign="LEFT")
            table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                       ("BOTTOMPADDING", (0, 0), (-1, -1), 12)]))
            if index == 0:
                story.append(KeepTogether(heading + [table]))
            else:
                story.append(table)

        story.append(paragraph("Green reference + ✓ = matched or successfully excluded; red reference + ? = needs review. Red strikeouts exclude purchases or sales tax."))
        errors = [["Location", "Unexplained amount"]]
        for data in receipt["text_geometry"]:
            for issue in data.get("verification_issues", []):
                errors.append([paragraph("Not found", "OtherProject"), paragraph(issue, "OtherProject")])
        if receipt["original_total"] is None:
            errors.append([paragraph("Not found", "OtherProject"), paragraph("Receipt total could not be read.", "OtherProject")])
        elif receipt["unresolved"]:
            errors.append([paragraph("Total", "OtherProject"), paragraph(
                f"Original receipt ${receipt['original_total']:,.2f}, less sales tax ${receipt['excluded_tax']:,.2f}, "
                f"differs from database purchases and shipping ${receipt['allocated_total']:,.2f} "
                f"by ${receipt['unresolved']:,.2f}.", "OtherProject")])
        for marks in annotations:
            for mark in marks:
                if mark["status"] == "?":
                    errors.append([paragraph(mark["id"], "OtherProject"), paragraph(mark["reason"], "OtherProject")])
        matched_refs = {mark.get("item_ref") for marks in annotations for mark in marks}
        if receipt.get("shipping") and "shipping" not in matched_refs:
            errors.append([paragraph("Not found", "OtherProject"), paragraph(
                f"Invoice Shipping field is ${receipt['shipping']:,.2f}; no shipping charge was located on the receipt.", "OtherProject")])
        for key, items in receipt.get("source_groups", receipt["groups"]).items():
            for i, item in enumerate(items):
                if item[0] != 0 and (key, i) not in matched_refs:
                    errors.append([paragraph("Not found", "OtherProject"), paragraph(
                        f"{item[1]}: listed in database as ${item[0]:,.2f}; no matching receipt item was found.", "OtherProject")])
        if len(errors) > 1:
            story.append(paragraph("Unexplained amounts", "OtherProjectHeading"))
            table = Table(errors, colWidths=[65, CONTENT_WIDTH - 65], repeatRows=1, splitInRow=1)
            table.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TEXTCOLOR", (0, 0), (-1, -1), OTHER_PROJECT_COLOR),
                ("BACKGROUND", (0, 0), (-1, 0), TABLE_HEADER_GRAY),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]))
            story.extend([table, Spacer(1, 12)])

    def item_locations(receipt, keys, project):
        marks = [mark for page in decisions[id(receipt), project] for mark in page]
        locations = []
        for key, i in keys:
            source_ref = receipt.get("source_refs", {}).get((key, i), (key, i))
            matching = [mark for mark in marks if mark.get("item_ref") == source_ref]
            labels = []
            for mark in matching:
                color = "#008000" if mark["status"] in ("ok", "strike") else "#b00020"
                symbol = "✓" if mark["status"] in ("ok", "strike") else "❓"
                label = escape(mark["id"]) + f' <font name="ReceiptSymbols">{symbol}</font>'
                if mark["status"] == "?":
                    label += f" (${mark['value']:,.2f})"
                labels.append(f'<font color="{color}">{label}</font>')
            locations.append(Paragraph("<br/>".join(labels) if labels else '<font color="#b00020">Not found</font>', styles["Normal"]))
        return locations

    project_totals = {}
    for receipt in receipts:
        for project, items in receipt["groups"].items():
            project_totals[project] = project_totals.get(project, Decimal(0)) + group_total(receipt, project)
    ordered_projects = sorted(project_totals, key=lambda item: (item[1].casefold(), item[0] or 0))

    def summary_table(headings, rows, total_label, total, receipt_amounts=None):
        data = [headings] + [[label if isinstance(label, Paragraph) else paragraph(label), f"{amount:,.2f}"] for label, amount in rows]
        data.append([total_label, f"{total:,.2f}"])
        widths = [CONTENT_WIDTH * .32, CONTENT_WIDTH * .18]
        if receipt_amounts is not None:
            for row, amount in zip(data[1:-1], receipt_amounts):
                row.insert(1, f"{amount:,.2f}" if amount is not None else "—")
            data[-1].insert(1, "")
            widths = [CONTENT_WIDTH * .5, CONTENT_WIDTH * .25, CONTENT_WIDTH * .25]
        table = Table(data, colWidths=widths,
                      hAlign="CENTER", repeatRows=1, splitInRow=1)
        table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("BACKGROUND", (0, 0), (-1, 0), TABLE_HEADER_GRAY),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ("LINEABOVE", (0, -1), (-1, -1), .7, colors.black),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ]))
        story.extend([table, Spacer(1, 16)])

    def purchase_section(receipt, key):
        # Both report orders render exactly the same receipt/project purchase.
        story.append(receipt_title(receipt, outline_level=1, project=key))
        if receipt.get("explanation"):
            story.extend([paragraph("Invoice explanation", "Heading3"),
                          paragraph(receipt["explanation"]), Spacer(1, 10)])
        if receipt.get("shipping"):
            story.append(paragraph(
                f"Invoice shipping: ${receipt['shipping']:,.2f}. Shipping is included below"
                + (" in proportion to each project's stored purchase value."
                   if len(receipt["groups"]) > 1 else ".")))
        purchases(receipt["groups"][key], allocations=[receipt["allocations"][key, i]
                  for i in range(len(receipt["groups"][key]))],
                  locations=item_locations(receipt, [(key, i) for i in range(len(receipt["groups"][key]))], key),
                  notes=[receipt.get("use_notes", {}).get(receipt.get("source_refs", {}).get((key, i), (key, i)), "")
                         for i in range(len(receipt["groups"][key]))])
        other_items, other_projects, other_allocations, other_keys = [], [], [], []
        for other_key, items in receipt["groups"].items():
            if other_key != key:
                other_items.extend(items)
                other_keys.extend((other_key, i) for i in range(len(items)))
                other_projects.extend([other_key[1]] * len(items))
                other_allocations.extend(receipt["allocations"][other_key, i] for i in range(len(items)))
        if other_items:
            story.append(paragraph(f"Purchases for other projects (excluded from total for {key[1]})", "OtherProjectHeading"))
            purchases(other_items, other_projects, other_allocations,
                      item_locations(receipt, other_keys, key),
                      notes=[receipt.get("use_notes", {}).get(receipt.get("source_refs", {}).get(ref, ref), "") for ref in other_keys])
        documents(receipt, [item[1] for item in other_items],
                  [item[1] for item in receipt["groups"][key]],
                  costs=[(item[0], item[3]) for item in other_items], project=key)

    def receipt_total(receipt):
        return receipt["allocated_total"]

    def recommended_corrections():
        recommendations = [(receipt, suggestion) for receipt in receipts
                           for suggestion in receipt.get("recommendations", [])]
        if not recommendations:
            return
        story.append(paragraph("Recommended corrections", "Heading2"))
        story.append(paragraph(
            "Review these suggestions against the original receipt before editing. "
            "Prices below are unit costs including attributable tariffs; database values are unchanged."))

        def money(value):
            if value is None:
                return "—"
            text = format(value, ",f")
            if "." in text:
                text = text.rstrip("0").rstrip(".")
            if "." not in text:
                text += ".00"
            elif len(text.rsplit(".", 1)[1]) < 2:
                text += "0"
            return "$" + text

        data = [[paragraph(label) for label in ["Receipt / project", "Item", "Unit cost: database → suggested", "Reason / evidence"]]]
        for receipt, suggestion in recommendations:
            current = money(suggestion["current_unit"])
            if suggestion["proposed_unit"] is not None:
                change = current + ' <font name="Symbol">→</font> ' + money(suggestion["proposed_unit"])
            elif suggestion["kind"] == "unbilled":
                change = current + ' <font name="Symbol">→</font> check invoice'
            elif suggestion["kind"] == "quantity":
                change = current + ' <font name="Symbol">→</font> check quantity'
            else:
                change = "Review tariff " + money(suggestion["tariff"])
            reason = suggestion["reason"]
            if suggestion["refs"]:
                reason += " Reference: " + suggestion["refs"] + "."
            data.append([
                paragraph(f"#{receipt['id']} / {suggestion['project']}"),
                paragraph(suggestion["item"]), Paragraph(change, styles["Normal"]), paragraph(reason)])
        table = Table(data, colWidths=[75, 135, 110, CONTENT_WIDTH - 320],
                      hAlign="LEFT", repeatRows=1, splitInRow=1)
        table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 0), (-1, 0), TABLE_HEADER_GRAY),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f8fa")]),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
        ]))
        story.extend([table, Spacer(1, 16)])

    yield progress.start("sections", section_count)
    story.append(paragraph("Report summary", "Heading1", outline_level=0))
    recommended_corrections()
    if grouping == "receipt":
        summary_table(["Receipt", "Amount"], [
            (Paragraph(receipt_label(receipt, short=True), styles["Normal"]), receipt_total(receipt))
            for receipt in receipts], "OVERALL TOTAL:", sum(project_totals.values(), Decimal(0)))
        for receipt in receipts:
            story.append(PageBreak())
            story.append(receipt_title(receipt, style="Heading1", outline_level=0))
            keys = [key for key in ordered_projects if key in receipt["groups"]]
            summary_table(["Project", "Amount"], [
                (key[1], group_total(receipt, key))
                for key in keys], "RECEIPT TOTAL:", receipt_total(receipt))
            for key in keys:
                purchase_section(receipt, key)
                yield progress.advance()
            if not keys:
                story.append(paragraph("No purchases are attached to this receipt."))
                documents(receipt)
                yield progress.advance()
    else:
        summary_table(["Project", "Amount"], [(key[1], project_totals[key]) for key in ordered_projects],
                      "OVERALL TOTAL:", sum(project_totals.values(), Decimal(0)))
        for key in ordered_projects:
            story.append(PageBreak())
            story.append(paragraph(key[1], "Heading1", outline_level=0))
            summary_table(["Receipt", "Receipt amount", "Relevant to project"], [
                (Paragraph(receipt_label(receipt, short=True), styles["Normal"]), group_total(receipt, key))
                for receipt in receipts if key in receipt["groups"]], "PROJECT TOTAL:", project_totals[key],
                receipt_amounts=[receipt["original_total"] for receipt in receipts if key in receipt["groups"]])
            for receipt in receipts:
                if key in receipt["groups"]:
                    purchase_section(receipt, key)
                    yield progress.advance()
        orphans = [receipt for receipt in receipts if not receipt["groups"]]
        if orphans:
            if story:
                story.append(PageBreak())
            story.append(paragraph("Receipts without purchases", "Heading1", outline_level=0))
            for receipt in orphans:
                documents(receipt)
                yield progress.advance()

    buf = BytesIO()
    doc = NavigableReport(buf, pagesize=letter, rightMargin=MARGIN, leftMargin=MARGIN,
                            topMargin=MARGIN, bottomMargin=MARGIN,
                            title=f"Receipts by {grouping}")
    yield progress.start("layout")
    doc.build(story)
    yield progress.advance()
    writer = PdfWriter()
    writer.append(PdfReader(BytesIO(buf.getvalue())))
    writer.page_mode = "/UseOutlines"
    output = BytesIO()
    yield progress.start("write")
    writer.write(output)
    yield progress.advance()
    return output.getvalue()


def generate(db, invoices, grouping):
    return _consume(generate_events(db, invoices, grouping))


def generate_events(db, invoices, grouping):
    """Yield progress snapshots; return the completed PDF as the generator result."""
    invoices = sorted(invoices, key=lambda invoice: invoice["id"])
    progress = ReportProgress(invoices)
    receipts = yield from _collect_receipts(db, invoices, progress)
    return (yield from _build_pdf(receipts, grouping, progress))
