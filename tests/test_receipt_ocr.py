"""OCR evidence, readable discrepancies, and the actual PDF overlay contract."""
from decimal import Decimal as D
from io import BytesIO
from types import SimpleNamespace

import pytest
from pypdf import PdfReader
from reportlab.pdfgen import canvas

import receipt_markup as markup
import receipt_reports as reports
from conftest import jpeg_images


def sample():
    buf = BytesIO()
    pdf = canvas.Canvas(buf, pagesize=(612, 792))
    pdf.drawString(40, 700, 'Broken text layer $999.99')
    pdf.save()
    return {'id': 9, 'pages': reports.document_pages(buf.getvalue()), 'groups': {
        (1, 'Research'): [(D('10.12'), 'Precision shielded cable', 1, D('10.12'))],
        (2, 'Office'): [(D('20.00'), 'Adjustable steel clamp', 1, D('20.00'))]}}


def spans():
    return [{'text': text, 'height': 10, 'confidence': 99, 'line': (40, y, 280, y)}
            for text, y in [('Precision shielded cable $12.10', 700),
                            ('Adjustable steel clamp $20.00', 660), ('Total $32.10', 620)]]


def test_every_page_uses_cached_ocr_and_ignores_corrupt_native_text(monkeypatch):
    calls = []
    monkeypatch.setattr(markup, 'ocr_spans', lambda p: calls.append(p) or spans())
    data = markup.reconcile(sample())
    for _ in range(2):
        decisions = markup.verify_amounts(data, (1, 'Research'))[0]
    assert len(calls) == 1
    assert [m['id'] for m in decisions] == ['p1.1', 'p1.2', 'p1.3']
    assert decisions[0]['item_ref'] == ((1, 'Research'), 0)
    assert decisions[0]['status'] == '?'
    assert 'listed in database as $10.12, but on receipt as $12.10' in decisions[0]['reason']
    assert decisions[1]['status'] == 'strike'
    assert data['original_total'] == D('32.10')
    assert all(m['value'] != D('999.99') for m in decisions)


@pytest.mark.parametrize('grouping', ['receipt', 'project'])
def test_item_locations_error_table_and_translucent_exclusion_marks(monkeypatch, grouping):
    monkeypatch.setattr(markup, 'ocr_spans', lambda p: spans())
    data = sample()
    original = data['pages'][0].get_contents().get_data()
    output = reports.build_pdf([data], grouping)
    reader = PdfReader(BytesIO(output))
    text = '\n'.join(p.extract_text() for p in reader.pages)
    assert 'Location' in text and 'Unexplained amounts' in text
    assert '✓' in text and '❓' in text
    assert 'listed in database as $10.12, but on receipt as $12.10' in ' '.join(text.split())
    assert 'p1.1' in text and 'p1.2' in text
    assert 'contextually' not in text
    seen_strike = seen_green_label = seen_red_question = False
    for page in reader.pages:
        state, stack = {}, []
        resources = page['/Resources']
        for args, op in page.get_contents().operations:
            if op == b'q':
                stack.append(dict(state))
            elif op == b'Q':
                state = stack.pop()
            elif op == b'gs':
                state.update(resources['/ExtGState'][args[0]].get_object())
            elif op in (b'rg', b'RG'):
                state[op] = tuple(float(v) for v in args)
            elif op == b're':
                state['path_kind'] = 'rectangle'
            elif op == b'm':
                state['path_kind'] = 'line'
            elif op == b'S' and state.get(b'RG', (0,))[0] > .6 and state.get('path_kind') == 'line':
                assert state.get('/CA') == .5
                seen_strike = True
            elif op == b'Tj' and 'p1.2' in str(args[0]) and state.get('/ca') == .5:
                assert state[b'rg'] == (0, 128/255, 0) or abs(state[b'rg'][1] - 128/255) < .00001
                seen_green_label = True
            elif op == b'Tj' and 'p1.1' in str(args[0]) and state.get('/ca') == .5:
                assert state[b'rg'][0] > .6
                seen_red_question = True
    assert seen_strike and seen_green_label and seen_red_question
    assert data['pages'][0].get_contents().get_data() == original


def test_ocr_failure_never_matches_native_gibberish(monkeypatch):
    monkeypatch.setattr(markup, 'ocr_spans', lambda page: [])
    data = markup.reconcile(sample())
    assert data['verification'] == [[]]
    text = '\n'.join(p.extract_text() for p in PdfReader(BytesIO(reports.build_pdf([data], 'receipt'))).pages)
    assert 'OCR could not read this receipt' in text
    assert 'no matching receipt item was found' in text
    assert 'Broken text layer $999.99' not in text  # source is a bitmap
    jpeg_images(PdfReader(BytesIO(reports.build_pdf([data], 'receipt'))))


def test_renderer_and_ocr_map_crop_coordinates_without_system_commands(monkeypatch):
    from tesserocr import RIL
    calls = []
    class Word:
        def GetUTF8Text(self, level): return '$12.10'
        def Confidence(self, level): return 95
        def BoundingBox(self, level): return (20, 30, 70, 40)
        def Next(self, level): return False
    class Engine:
        def SetImage(self, image): calls.append(image.size)
        def Recognize(self): pass
        def GetIterator(self): return Word()
        def Clear(self): pass
    monkeypatch.setattr(markup, '_ocr_engine', Engine())
    page = sample()['pages'][0]
    page.cropbox.lower_left = (12, 24)
    page.cropbox.upper_right = (212, 424)
    result = markup.ocr_spans(page)
    assert calls == [(1000,2000)]
    assert result[0]['confidence'] == 95
    assert result[0]['line'][0] == pytest.approx(12 + 20/5)
    assert result[0]['line'][1] == pytest.approx(424 - 35.5/5)


def test_digikey_wrapped_description_and_vendor_model_suffix(monkeypatch):
    recognized = [
        {'text': '505-DC2645A-ND', 'height': 8, 'line': (40, 710, 140, 710)},
        {'text': 'LTC5594 L 300MHZ TO 9GHZ I/Q DEM', 'height': 8, 'line': (40, 690, 280, 690)},
        {'text': '320.84000', 'height': 8, 'line': (400, 700, 450, 700)},
        {'text': '$320.84', 'height': 8, 'line': (500, 700, 550, 700)},
        {'text': '1568-20359-ND', 'height': 8, 'line': (40, 660, 140, 660)},
        {'text': 'TEENSY 4.1 WITHOUT ETHERNET', 'height': 8, 'line': (40, 640, 280, 640)},
        {'text': '29.60000', 'height': 8, 'line': (400, 650, 450, 650)},
        {'text': '$29.60', 'height': 8, 'line': (500, 650, 550, 650)},
        {'text': 'Subtotal: $350.44', 'height': 8, 'line': (300, 600, 450, 600)},
        {'text': 'Shipping: $8.49', 'height': 8, 'line': (300, 580, 450, 580)},
        {'text': 'Sales Tax: $28.71', 'height': 8, 'line': (300, 560, 450, 560)},
        {'text': 'Total: $387.64', 'height': 8, 'line': (300, 540, 450, 540)},
    ]
    monkeypatch.setattr(markup, 'ocr_spans', lambda p: recognized)
    data = sample()
    data['shipping'] = D('8.49')
    data['groups'] = {(1, 'Research'): [
        (D('320.84'), 'LTC5594 eval board', 1, D('320.84')),
        (D('29.60'), 'SparkFun Electronics DEV-20359', 1, D('29.60'))]}
    markup.reconcile(data)
    decisions = markup.verify_amounts(data)[0]
    assert [m['status'] for m in decisions] == ['ok'] * 6 + ['strike', 'ok']
    assert data['original_total'] == D('387.64')
    assert data['reimbursable_total'] == D('358.93')


def test_real_ocr_reads_image_only_receipt():
    import pypdfium2 as pdfium
    buf = BytesIO()
    pdf = canvas.Canvas(buf, pagesize=(612, 792))
    pdf.drawString(40, 700, 'Precision shielded cable $12.10')
    pdf.drawString(40, 650, 'Total $12.10')
    pdf.save()
    with pdfium.PdfDocument(buf.getvalue()) as document:
        bitmap = document[0].render(scale=2)
        try:
            png = BytesIO()
            bitmap.to_pil().save(png, format='PNG')
        finally:
            bitmap.close()
    data = sample()
    data['pages'] = reports.document_pages(png.getvalue())
    data['groups'] = {(1, 'Research'): [(D('10.12'), 'Precision shielded cable', 1, D('10.12'))]}
    assert not data['pages'][0].extract_text().strip()
    markup.reconcile(data)
    decisions = markup.verify_amounts(data)[0]
    assert [m['value'] for m in decisions] == [D('12.10'), D('12.10')]
    assert decisions[0]['item_ref'] == ((1, 'Research'), 0)
    assert 'listed in database as $10.12, but on receipt as $12.10' in decisions[0]['reason']
    assert data['original_total'] == D('12.10')


def test_likely_reordered_name_matches_word_level_ocr_even_with_wrong_amount(monkeypatch):
    words = 'YSAGi Large Extended Leather Desk Pad Protector $15.99'.split()
    recognized = [{'text': word, 'height': 10, 'confidence': 99,
                   'line': (40+i*45, 700, 80+i*45, 700)} for i, word in enumerate(words)]
    monkeypatch.setattr(markup, 'ocr_spans', lambda page: recognized)
    data = sample()
    data['groups'] = {(1, 'Research'): [(D('14.39'), 'YSAGi Leather Office Desk Pad Protector', 1, D('14.39'))]}
    markup.reconcile(data)
    decision = markup.verify_amounts(data)[0][0]
    assert decision['item_ref'] == ((1, 'Research'), 0)
    assert decision['status'] == '?'
    assert 'listed in database as $14.39, but on receipt as $15.99' in decision['reason']


def test_word_boxes_preserve_currency_and_labeled_whole_dollar_amounts():
    recognized = [
        {'text': word, 'height': 10, 'line': (x, y, x+30, y)}
        for word, x, y in [('Quantity', 40, 740), ('13', 100, 740),
                           ('$', 40, 700), ('10', 80, 700),
                           ('Shipping:', 40, 660), ('2', 100, 660),
                           ('Tax:', 40, 620), ('1', 100, 620),
                           ('Total', 40, 580), ('13', 100, 580)]]
    assert [p['value'] for p in markup._prices(recognized)] == [D('10'), D('2'), D('1'), D('13')]
