"""Filtered receipt summaries with original supporting pages embedded as PDF content."""
from collections import OrderedDict
from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from io import BytesIO
import ipaddress
import socket
from urllib.parse import urljoin, urlparse
from xml.sax.saxutils import escape

import requests
from PIL import Image as PILImage
from pypdf import PdfReader, PdfWriter, Transformation
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    Flowable, HRFlowable, PageBreak, Paragraph, SimpleDocTemplate, Spacer,
    Table, TableStyle,
)

import quotes

PAGE_WIDTH, PAGE_HEIGHT = letter
MARGIN = 36
CONTENT_WIDTH = PAGE_WIDTH - 2 * MARGIN
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_DOCUMENT_PAGES = 100
CENT = Decimal("0.01")


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
    for _ in range(6):
        _public_url(url)
        with requests.get(url, stream=True, timeout=(5, 20), allow_redirects=False,
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
    return {"invoice_id": invoice["id"], "nickname": invoice["nickname"],
            "message": f'Receipt #{invoice["id"]} ({invoice["nickname"]}): {reason} {action}'}


def collect_receipts(db, invoices):
    receipts, errors, cache = [], [], {}
    for invoice in invoices:
        rows = db.execute(
            """SELECT o.id, o.description, o.cost, o.quantity, o.project_id,
                      COALESCE(p.name, 'Unassigned project') AS project_name
               FROM orders o LEFT JOIN projects p ON p.id = o.project_id
               WHERE o.invoice_id = ? ORDER BY o.id""", (invoice["id"],)).fetchall()
        groups = OrderedDict()
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
            groups.setdefault(key, []).append((amount, row["description"], row["quantity"], cost))
        url = (invoice["receipt_url"] or "").strip() or (invoice["invoice_url"] or "").strip()
        if url not in cache:
            try:
                if not url:
                    raise ValueError("No receipt or invoice URL is recorded.")
                cache[url] = (document_pages(download_document(url)), None)
            except Exception as exc:
                # Do not expose share URLs/tokens or remote exception bodies.
                reason = str(exc) if isinstance(exc, ValueError) else "Could not download or read the supporting file. Check its sharing permissions and file format."
                cache[url] = (None, reason)
        pages, failure = cache[url]
        if failure:
            errors.append(_error(invoice, failure))
        receipts.append({"id": invoice["id"], "groups": groups, "pages": pages})
    if errors:
        raise ReportError(errors)
    return receipts


class SupportingPage(Flowable):
    """Reserve layout space, then merge the source page after pagination."""
    def __init__(self, page, placements):
        super().__init__()
        self.page, self.placements = page, placements
        width, height = float(page.cropbox.width), float(page.cropbox.height)
        self.scale = min(1, (CONTENT_WIDTH / 2 - 12) / width, 600 / height)
        self.width, self.height = width * self.scale, height * self.scale

    def draw(self):
        x, y = self.canv.absolutePosition(0, 0)
        self.placements.append((self.canv.getPageNumber() - 1, x, y, self))
        self.canv.setStrokeColor(colors.black)
        self.canv.setLineWidth(1)
        self.canv.rect(0, 0, self.width, self.height)


def build_pdf(receipts, grouping):
    styles = getSampleStyleSheet()
    placements, story = [], []

    def paragraph(text, style="Normal"):
        return Paragraph(escape(str(text)).replace("\n", "<br/>"), styles[style])

    def purchases(items):
        rows = [["Description", "Quantity", "Unit cost", "Line total"]]
        rows.extend([[paragraph(description), str(quantity), f"{cost:,.2f}", f"{amount:,.2f}"]
                     for amount, description, quantity, cost in items])
        total = sum((item[0] for item in items), Decimal(0))
        rows.append(["TOTAL:", "", "", f"{total:,.2f}"])
        table = Table(rows, colWidths=[CONTENT_WIDTH - 230, 60, 80, 90],
                      hAlign="LEFT", repeatRows=1, splitInRow=1)
        table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e9edf2")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("LINEBELOW", (0, 0), (-1, 0), 0.7, colors.black),
            ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#f7f8fa")]),
            ("LINEABOVE", (0, -1), (-1, -1), 0.7, colors.black),
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ("SPAN", (0, -1), (2, -1)),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ]))
        story.extend([table, Spacer(1, 12)])

    def documents(receipt):
        story.append(paragraph(f'Supporting document — Receipt #{receipt["id"]}', "Heading3"))
        pages = receipt["pages"]
        for index in range(0, len(pages), 2):
            cells = [SupportingPage(page, placements) for page in pages[index:index + 2]]
            if len(cells) == 1:
                cells.append("")
            table = Table([cells], colWidths=[CONTENT_WIDTH / 2] * 2, hAlign="LEFT")
            table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                       ("BOTTOMPADDING", (0, 0), (-1, -1), 12)]))
            story.append(table)

    if grouping == "receipt":
        for receipt in receipts:
            if story:
                story.append(PageBreak())
            story.append(paragraph(f'Receipt #{receipt["id"]}: This comprises receipts from the following projects:', "Heading2"))
            for (_, name), items in receipt["groups"].items():
                story.append(paragraph(name + ":", "Heading3"))
                purchases(items)
            if not receipt["groups"]:
                story.append(paragraph("No purchases are attached to this receipt."))
            documents(receipt)
    else:
        projects = {key for receipt in receipts for key in receipt["groups"]}
        for key in sorted(projects, key=lambda item: (item[1].casefold(), item[0] or 0)):
            if story:
                story.append(PageBreak())
            story.append(paragraph(key[1], "Heading1"))
            for _ in range(2):
                story.append(HRFlowable(width="100%", thickness=1, color=colors.black, spaceAfter=3))
            for receipt in receipts:
                if key not in receipt["groups"]:
                    continue
                story.append(paragraph(f'Receipt #{receipt["id"]}', "Heading2"))
                statement = ("All purchases on this receipt are relevant to this project"
                             if len(receipt["groups"]) == 1 else
                             "Only the following purchases on this receipt are relevant to this project.")
                story.extend([paragraph(statement), Spacer(1, 10)])
                purchases(receipt["groups"][key])
                documents(receipt)
        orphans = [receipt for receipt in receipts if not receipt["groups"]]
        if orphans:
            if story:
                story.append(PageBreak())
            story.append(paragraph("Receipts without purchases", "Heading1"))
            for receipt in orphans:
                documents(receipt)

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, rightMargin=MARGIN, leftMargin=MARGIN,
                            topMargin=MARGIN, bottomMargin=MARGIN,
                            title=f"Receipts by {grouping}")
    doc.build(story)
    writer = PdfWriter()
    writer.append(PdfReader(BytesIO(buf.getvalue())))
    for page_index, x, y, flowable in placements:
        source = deepcopy(flowable.page)
        transform = Transformation().translate(-float(source.cropbox.left), -float(source.cropbox.bottom))
        transform = transform.scale(flowable.scale).translate(x, y)
        writer.pages[page_index].merge_transformed_page(source, transform, over=False)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def generate(db, invoices, grouping):
    return build_pdf(collect_receipts(db, invoices), grouping)
