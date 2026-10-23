"""Excluded purchases are struck on the supporting page, with relevant rows protected."""
from io import BytesIO
from decimal import Decimal
from datetime import date
from types import SimpleNamespace

from pypdf import PdfReader
from reportlab.pdfgen import canvas

import receipt_markup as markup
import receipt_reports as reports


def sample_page():
    buf = BytesIO()
    pdf = canvas.Canvas(buf, pagesize=(612, 792))
    pdf.drawString(40, 740, 'Order placed October 6, 2026')
    pdf.drawString(40, 700, 'KARACA Urban Stainless Steel Induction Turkish Tea Kettle Set')
    pdf.drawString(40, 660, 'SEESII NanoVNA-H4 Vector Network Analyzer with 32GB Card')
    pdf.drawString(40, 620, 'PART: DS18B20+PAR-ND SENSORDIGITAL TO92-3')
    pdf.drawString(40, 580, 'PART: 505-EVAL-AD5941ELCZ-ND')
    pdf.drawString(40, 540, 'PART: 296-TLV333IDRCT-ND')
    pdf.save()
    return reports.document_pages(buf.getvalue())[0]


def test_native_regex_matching_uses_positions_and_model_numbers():
    page = sample_page()
    lines, missing = markup.strike_through([page], [
        'SEESII Upgraded NanoVNA-H4 Vector Network Analyzer, Latest V4.4',
        'DS18B20+PAR Analog Devices Inc./Maxim Integrated | DigiKey',
    ], ['Amazon.com | KARACA Urban Stainless Steel Induction Turkish Tea Kettle Set'])
    assert not missing
    assert len(lines[0]) == 2
    assert all(620 < y < 670 for _, y, _, _ in lines[0])


def test_common_description_is_not_used_to_strike_relevant_items():
    page = sample_page()
    lines, missing = markup.strike_through([page], [
        'KARACA Urban Stainless Steel Induction Turkish Tea Kettle Set',
    ], ['KARACA Urban Stainless Steel Induction Turkish Tea Kettle Set'])
    assert lines == [[]]
    assert len(missing) == 1


def test_device_names_match_package_suffixes_on_digikey_receipts():
    lines, missing = markup.strike_through([sample_page()],
                                         ['AD5941 development board', 'TLV333'], [])
    assert not missing
    assert len(lines[0]) == 2
    assert all(540 < y < 590 for _, y, _, _ in lines[0])


def test_no_confident_match_is_reported_and_scans_use_optional_ocr(monkeypatch):
    real_ocr = markup.ocr_spans
    monkeypatch.setattr(markup, 'text_spans', lambda page: [])
    calls = []
    def ocr(page):
        calls.append(page)
        return [{'text': 'PART: DS18B20+PAR-ND', 'line': (40, 700, 190, 700)}]
    monkeypatch.setattr(markup, 'ocr_spans', ocr)
    geometry = [{'native': []}]
    for _ in range(2):
        lines, missing = markup.strike_through([object()], ['DS18B20+PAR temperature sensor'], [], geometry)
        assert lines == [[(40, 700, 190, 700)]]
        assert not missing
    assert len(calls) == 1
    monkeypatch.setattr(markup.shutil, 'which', lambda name: None)
    assert real_ocr(object()) == []


def test_ocr_pipeline_uses_memory_and_maps_pixel_coordinates(monkeypatch):
    from PIL import Image
    image = BytesIO()
    Image.new('RGB', (100, 200), 'white').save(image, format='PNG')
    tsv = 'level\tleft\ttop\twidth\theight\tconf\ttext\n5\t20\t30\t50\t10\t95\tDS18B20\n'
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs['input']))
        return SimpleNamespace(stdout=image.getvalue() if len(calls) == 1 else tsv.encode())
    monkeypatch.setattr(markup.shutil, 'which', lambda name: '/usr/bin/' + name)
    monkeypatch.setattr(markup.subprocess, 'run', run)
    spans = markup.ocr_spans(sample_page())
    assert spans[0]['text'] == 'DS18B20'
    assert abs(spans[0]['line'][0] - 122.4) < .01
    assert abs(spans[0]['line'][1] - 651.42) < .01
    assert calls[0][0][-1] == '-'
    assert calls[0][1].startswith(b'%PDF')
    assert calls[1][0][1:3] == ['stdin', 'stdout']
    assert calls[1][1] == image.getvalue()


def test_red_lines_are_only_on_project_receipt_images_and_sources_unchanged():
    page = sample_page()
    receipts = [{'id': 20, 'vendor': 'Amazon', 'transaction_date': date(2026, 10, 6),
        'provided_by': 'person@email.com', 'pages': [page], 'groups': {
            (1, 'Office'): [(Decimal('44.99'), 'KARACA Urban Stainless Steel Induction Turkish Tea Kettle Set', 1, Decimal('44.99'))],
            (2, 'Test'): [(Decimal('79.18'), 'SEESII NanoVNA-H4 Vector Network Analyzer', 1, Decimal('79.18'))]}}]
    original = page.get_contents().get_data()
    def red_strokes(data):
        strokes = []
        for output_page in PdfReader(BytesIO(data)).pages:
            red = False
            stack = []
            for operands, operator in output_page.get_contents().operations:
                if operator == b'q': stack.append(red)
                elif operator == b'Q': red = stack.pop() if stack else False
                elif operator == b'RG': red = float(operands[0]) > .6 and float(operands[1]) == 0
                elif operator == b'l' and red: strokes.append(operands)
        return strokes
    assert len(red_strokes(reports.build_pdf(receipts, 'project'))) == 2
    assert not red_strokes(reports.build_pdf(receipts, 'receipt'))
    assert page.get_contents().get_data() == original
