"""Regression evidence from the supplied DigiKey invoice, without customer data."""
from copy import deepcopy
from decimal import Decimal as D
from io import BytesIO
import json
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter

import receipt_markup as markup
import receipt_reports as reports
from conftest import jpeg_images


def digikey_receipt():
    recognized = json.loads((Path(__file__).parent / 'fixtures' / 'digikey_invoice_review.json').read_text())
    writer = PdfWriter()
    for _ in recognized:
        writer.add_blank_page(width=792, height=612)
    pdf = BytesIO()
    writer.write(pdf)
    groups = {(1, 'C-band'): [], (2, 'ODNP'): []}
    for name, qty, cost in [
        ('ADF4372 eval board', 1, '757.86'),
        ('Connor-Winfield OH100-50503CF-010.0M', 1, '82.72'),
        ('TLV333', 2, '1.03'), ('AH49HZ3-G1', 3, '1.04'),
        ('AD8420ARMZ-R7', 2, '3.64'), ('AD5941 development board', 1, '416.00'),
        ('EVAL-ADICUP3029 (MCU for AD5941)', 1, '65.00'),
        ('ADS127L21 eval board', 1, '394.00'), ('AD7902 dev board', 1, '140.00')]:
        groups[1, 'C-band'].append((D(cost) * qty, name, qty, D(cost)))
    groups[2, 'ODNP'] = [(D('41.20'), 'DS18B20+PAR Analog Devices Inc./Maxim Integrated', 5, D('8.24'))]
    for spans in recognized:
        for span in spans:
            span['line'] = tuple(span['line'])
    return dict(id=1, nickname='DigiKey', groups=groups, shipping=D('8.49'),
                pages=reports.document_pages(pdf.getvalue()),
                text_geometry=[dict(ocr=spans) for spans in recognized])


def test_receipt_one_has_real_partial_shipment_gap_and_exact_tariffs():
    data = markup.reconcile(digikey_receipt())
    assert data['original_total'] == D('1562.82')
    assert data['allocated_total'] == D('1917.73')
    assert data['unresolved'] == D('-354.91')
    assert sum(value for _, kind, value in data['charges'] if kind == 'tariff') == D('127.39')
    assert len(data['recommendations']) == 10
    suggestions = {r['item']: r for r in data['recommendations']}
    assert suggestions['ADS127L21 eval board']['kind'] == 'unbilled'
    assert suggestions['Connor-Winfield OH100-50503CF-010.0M']['kind'] == 'unbilled'
    assert suggestions['ADS127L21 eval board']['proposed_unit'] is None
    assert suggestions['AD7902 dev board']['proposed_unit'] == D('151.05')
    assert suggestions['AD5941 development board']['proposed_unit'] == D('449.52')
    assert suggestions['EVAL-ADICUP3029 (MCU for AD5941)']['proposed_unit'] == D('83.10')
    assert suggestions['AH49HZ3-G1']['proposed_unit'] == D('.64667')
    assert sum(r['proposed_total'] for r in suggestions.values() if r['kind'] == 'tariff') == D('1554.33')
    for suggestion in suggestions.values():
        if suggestion['proposed_unit'] is not None:
            assert (suggestion['proposed_unit'] * suggestion['quantity']).quantize(D('.01')) == suggestion['proposed_total']


def test_landed_costs_verify_after_invoice_assignments_and_prices_are_corrected():
    data = markup.reconcile(digikey_receipt())
    suggestions = {r['item']: r for r in data['recommendations']}
    corrected = {}
    for key, items in data['source_groups'].items():
        corrected[key] = []
        for _, name, qty, _ in items:
            suggestion = suggestions[name]
            if suggestion['kind'] != 'unbilled':
                corrected[key].append((suggestion['proposed_total'], name, qty, suggestion['proposed_unit']))
    data['source_groups'] = corrected
    markup.reconcile(data)
    assert data['unresolved'] == 0
    assert data['recommendations'] == []
    annotations = markup.verify_amounts(data)
    assert all(mark['status'] != '?' for page in annotations for mark in page)
    assert all(mark['status'] == 'strike' for page in annotations for mark in page if mark.get('unbilled'))
    assert all(mark['status'] == 'ok' for page in annotations for mark in page if mark['role'] == 'tariff')


@pytest.mark.parametrize('grouping', ['receipt', 'project'])
def test_pdf_has_amount_columns_without_recommendations_or_data_changes(grouping):
    data = markup.reconcile(digikey_receipt())
    before = deepcopy(data['source_groups'])
    output = reports.build_pdf([data], grouping)
    reader = PdfReader(BytesIO(output))
    first = reader.pages[0].extract_text()
    assert 'Recommended corrections' not in first
    text = '\n'.join(page.extract_text() for page in reader.pages)
    assert 'Recommended corrections' not in text
    assert 'Database amount' in text and 'Receipt amount' in text
    assert 'Not confirmed' not in text
    assert 'Receipt check:' in text
    assert data['source_groups'] == before
    assert reader.outline[0].title == 'Report summary'
    jpeg_images(reader)


def test_bitmap_is_the_same_cached_raster_from_ocr_and_reused_across_projects(monkeypatch):
    from reportlab.pdfgen import canvas
    original = BytesIO()
    pdf = canvas.Canvas(original)
    pdf.drawString(40, 700, 'Native source text must not be merged')
    pdf.save()
    image = Image.new('RGB', (300, 400), '#ffdd99')
    encoded = BytesIO()
    image.save(encoded, format='JPEG', quality=85)
    jpeg = encoded.getvalue()
    monkeypatch.setattr(markup, 'ocr_spans', lambda page: markup.RecognizedPage([], jpeg))
    data = dict(id=1, pages=reports.document_pages(original.getvalue()), groups={
        (1, 'A'): [(D(1), 'Cable', 1, D(1))], (2, 'B'): [(D(2), 'Adapter', 1, D(2))]})
    markup.reconcile(data)
    monkeypatch.setattr(markup, 'page_jpeg', lambda page: pytest.fail('OCR raster must be reused'))
    reader = PdfReader(BytesIO(reports.build_pdf([data], 'project')))
    images = jpeg_images(reader)
    assert len(images) == 1
    assert images[0].get_data() == jpeg
    assert images[0]['/Width'] == 300 and images[0]['/Height'] == 400
    assert 'Native source text' not in '\n'.join(p.extract_text() for p in reader.pages)


def test_measurement_is_not_money_and_tariff_summary_is_not_a_second_charge():
    spans = [dict(text='1.64', line=(40, 100, 60, 100), height=10),
             dict(text='kgs', line=(65, 100, 85, 100), height=10)]
    assert markup._prices(spans) == []
    data = markup.reconcile(digikey_receipt())
    roles = {price['value']: price['role'] for price in data['verification'][2]}
    assert roles[D('1554.33')] == 'before_tax'
    assert roles[D('127.39')] == 'tariff_total'
    assert roles[D('1426.94')] == 'merchandise'


@pytest.mark.parametrize('grouping', ['receipt', 'project'])
def test_receipt_sections_are_in_ascending_receipt_number(monkeypatch, grouping):
    monkeypatch.setattr(markup, 'ocr_spans', lambda page: [])
    source = digikey_receipt()['pages'][:1]
    receipts = [dict(id=i, groups={(1, 'Research'): [(D(1), 'Cable', 1, D(1))]}, pages=source)
                for i in [9, 2, 1]]
    reader = PdfReader(BytesIO(reports.build_pdf(receipts, grouping)))

    def titles(entries):
        for entry in entries:
            if isinstance(entry, list):
                yield from titles(entry)
            else:
                yield entry.title

    receipt_titles = [t for t in titles(reader.outline) if t.startswith('Receipt #')]
    assert list(dict.fromkeys(t.split(':')[0] for t in receipt_titles)) == ['Receipt #1', 'Receipt #2', 'Receipt #9']


def test_subcent_unit_costs_can_be_saved_and_displayed_without_losing_tariff_cents():
    import app as app_module
    assert app_module._normalise_cost('0.64667') == '0.64667'
    assert app_module.fmt_cost('0.64667') == '0.64667'
    assert app_module._normalise_cost('1,234.56000') == '1234.56'
    assert app_module.fmt_cost('1234.56') == '1,234.56'
    assert (D(app_module._normalise_cost('0.64667')) * 3).quantize(D('.01')) == D('1.94')


def test_targeted_ocr_recovers_zero_shipped_and_zero_billed_cells():
    from reportlab.pdfgen import canvas
    from tesserocr import PyTessBaseAPI, PSM
    buf = BytesIO()
    pdf = canvas.Canvas(buf, pagesize=(792, 612))
    pdf.setFont('Courier', 8)
    pdf.drawString(176, 300, '0')
    pdf.drawString(740, 300, '0.00')
    pdf.save()
    page = reports.document_pages(buf.getvalue())[0]
    spans = [dict(text=text, line=(x, y, right, y), height=6)
             for text, x, y, right in [('Shipped', 162, 350, 194), ('Unit', 678, 350, 693),
                                       ('Amount', 735, 350, 766), ('139.86000', 678, 300, 715)]]
    with markup.page_raster(page) as image:
        with PyTessBaseAPI(path=str(Path(markup.__file__).with_name('ocr_data')),
                            lang='eng', psm=PSM.AUTO) as engine:
            engine.SetImage(image)
            result = markup._table_cells(engine, spans, page.cropbox, .2, .2,
                                         image.width, image.height, PSM)
    assert {s['column']: s['text'] for s in result} == {'shipped': '0', 'amount': '0.00'}


@pytest.mark.parametrize('description,duplicate,expected', [
    ('Precision cable', False, True), ('Unrelated purchase', False, False),
    ('Precision cable', True, False)])
def test_close_prices_require_unique_product_evidence(description, duplicate, expected):
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    pdf = BytesIO()
    writer.write(pdf)
    items = [(D('10.00'), description, 1, D('10.00'))]
    if duplicate:
        items.append((D('11.00'), description, 1, D('11.00')))
    data = dict(id=2, pages=reports.document_pages(pdf.getvalue()), groups={(1, 'Research'): items},
                text_geometry=[dict(ocr=[dict(text='Precision cable $10.12',
                                              line=(40, 700, 240, 700), height=10)])])
    markup.reconcile(data)
    assert bool(data['recommendations']) == expected
    if expected:
        assert data['recommendations'][0]['proposed_unit'] == D('10.12')


def test_receipt_amounts_keep_zero_billed_separate_from_price_mismatches():
    import receipt_review
    data = markup.reconcile(digikey_receipt())
    key = (1, 'C-band')
    assert data['receipt_amounts'][key, 7] == 0  # ADS127L21
    assert data['receipt_amounts'][key, 1] == 0  # Connor-Winfield
    assert data['receipt_amounts'][key, 4] == D('7.86')  # AD8420: positive price mismatch
    assert data['groups'][key][4][0] == D('7.28')
    assert receipt_review.amount_sum(data['receipt_amounts'].values()) == D('1562.82')
    shipping = [(ref, value) for ref, value in data['receipt_amounts'].items()
                if data['source_refs'][ref] == 'shipping']
    assert [value for _, value in shipping] == [D('8.27'), D('.22')]
    marks = [p for page in markup.verify_amounts(data) for p in page]
    assert all(p['status'] == '?' for p in marks if p.get('item_ref') == (key, 4))


def test_balancing_total_never_turns_an_unmatched_item_into_zero():
    import receipt_review
    data = digikey_receipt()
    key = (1, 'Research')
    data['groups'] = {key: [(D(10), 'Precision cable', 1, D(10)),
                            (D(5), 'Unmatched adapter', 1, D(5))]}
    data['shipping'] = D(0)
    data['text_geometry'] = [dict(ocr=[dict(text=text, line=(40, y, 240, y), height=10)
                                     for text, y in [('Precision cable $10.00', 700), ('Total $10.00', 650)]])]
    data['pages'] = data['pages'][:1]
    markup.reconcile(data)
    assert data['original_total'] == 10
    assert data['receipt_amounts'][key, 0] == 10
    assert data['receipt_amounts'][key, 1] is None
    assert receipt_review.amount_sum(data['receipt_amounts'].values()) == 10


def test_explicit_total_wins_over_a_subtotal_matching_the_database():
    data = digikey_receipt()
    data['groups'] = {(1, 'Research'): [(D(10), 'Precision cable', 1, D(10))]}
    data['shipping'] = D(0)
    data['text_geometry'] = [dict(ocr=[dict(text=text, line=(40, y, 260, y), height=10)
                                     for text, y in [('Precision cable $10.00', 700),
                                                     ('Total before tax $10.00', 650),
                                                     ('Grand Total $11.00', 600)]])]
    data['pages'] = data['pages'][:1]
    markup.reconcile(data)
    assert data['original_total'] == 11
    assert data['unresolved'] == 1
    assert [price['value'] for price in data['total_occurrences']] == [D(11)]


def test_receipt_total_has_red_box_and_half_size_annotations():
    data = markup.reconcile(digikey_receipt())
    reader = PdfReader(BytesIO(reports.build_pdf([data], 'receipt')))
    fonts, rectangles = [], []
    for page in reader.pages:
        page.extract_text(visitor_text=lambda text, cm, tm, font, size: fonts.append((text.strip(), size)))
        stroke = (0, 0, 0)
        stack = []
        for args, op in page.get_contents().operations:
            if op == b'q': stack.append(stroke)
            elif op == b'Q': stroke = stack.pop()
            elif op == b'RG': stroke = tuple(float(x) for x in args)
            elif op == b're' and stroke[0] > .6 and stroke[1] == 0:
                rectangles.append(args)
    assert rectangles
    total_labels = [(text, size) for text, size in fonts if text.startswith('total p3.')]
    assert total_labels and all(size == 2.5 for _, size in total_labels)
    assert any(text.startswith('p1.1') and size == 2.5 for text, size in fonts)
    assert any(text.startswith('p1.1') and size == 5 for text, size in fonts)


def test_timing_log_compares_each_prediction_to_actual_remaining_time(monkeypatch):
    now = [0.]
    monkeypatch.setattr(reports, 'monotonic', lambda: now[0])
    progress = reports.ReportProgress([{}])
    progress.totals = dict.fromkeys(progress.totals, 0)
    progress.start('ocr', 4)
    for step in range(1, 5):
        now[0] = step * 6
        progress.advance()
    timing = progress.timing_log()
    assert timing['actual_seconds'] == 24
    assert timing['initial_estimated_seconds'] == 22.8
    assert timing['stages'][0]['predicted_seconds'] == 22.8
    assert timing['stages'][0]['actual_seconds'] == 24
    assert timing['updates'][0]['prediction_error_seconds'] == -1.2
    assert timing['updates'][1]['actual_remaining_seconds'] == 18
    assert timing['updates'][1]['prediction_error_seconds'] == 0
    assert timing['updates'][-1]['actual_remaining_seconds'] == 0
    json.dumps(timing)


def pasternack_receipt():
    spans = json.loads((Path(__file__).parent / 'fixtures' / 'pasternack_invoice_review.json').read_text())
    data = digikey_receipt()
    data.update(id=5, nickname='Pasternack', shipping=D(0), pages=data['pages'][:2],
                groups={(1, 'Research'): [(D('400.39'), 'PE83CR006', 1, D('400.39'))]},
                text_geometry=[dict(ocr=[dict(s, line=tuple(s['line'])) for s in page]) for page in spans])
    return data


@pytest.mark.parametrize('grouping', ['receipt', 'project'])
def test_pasternack_ocr_price_shipping_and_zero_tariff(grouping):
    data = markup.reconcile(pasternack_receipt())
    key = (1, 'Research')
    assert data['receipt_amounts'][key, 0] == D('401.99')
    assert data['receipt_amounts'][key, 1] == D('9.99')
    assert data['groups'][key][1][0] == 0  # receipt-only shipping
    suggestions = {r['kind']: r for r in data['recommendations']}
    assert set(suggestions) == {'price', 'shipping'}
    assert suggestions['price']['current_total'] == D('400.39')
    assert suggestions['price']['proposed_total'] == D('401.99')
    assert suggestions['price']['tariff'] == 0
    assert suggestions['shipping']['current_total'] == 0
    assert suggestions['shipping']['proposed_total'] == D('9.99')
    assert suggestions['shipping']['reason'] == (
        'Shipping from this receipt has not been entered for this invoice. '
        'Enter $9.99 in the invoice Shipping field.')
    assert suggestions['shipping']['refs']
    pdf = reports.build_pdf([data], grouping)
    first = PdfReader(BytesIO(pdf)).pages[0].extract_text()
    assert 'Awaiting shipment' in first and 'Discrepancy' in first
    assert '411.98' in first and '11.59' in first
    assert data['source_groups'][key][0][0] == D('400.39')
    assert data['shipping'] == 0


def test_ambiguous_ocr_part_substitution_cannot_suggest_item_price():
    data = pasternack_receipt()
    data['groups'][2, 'Other'] = [(D('400.39'), 'PE83CRO006', 1, D('400.39'))]
    markup.reconcile(data)
    assert not any(r['kind'] == 'price' for r in data['recommendations'])
    assert all(data['receipt_amounts'][key, 0] is None for key in data['source_groups'])


def test_supported_shipping_ignores_unmatched_items_and_preserves_cents():
    import receipt_review as review
    data = pasternack_receipt()
    data['groups'] = {(1, 'Research'): [(D('400.39'), 'PE83CR006', 1, D('400.39')),
                                      (D('100'), 'Unmatched adapter', 1, D(100))],
                      (2, 'Other'): [(D(20), 'Precision cable', 1, D(20))]}
    data['text_geometry'][0]['ocr'].append(dict(text='Precision cable $20.00', line=(40,600,240,600), height=10))
    markup.reconcile(data)
    shipping = [value for ref, value in data['receipt_amounts'].items() if data['source_refs'][ref] == 'shipping']
    assert shipping == [D('9.52'), D('.47')]
    assert sum(shipping) == D('9.99')
    assert review.amount_sum(data['receipt_amounts'].values()) == D('431.98')
    assert review.awaiting_shipment(data, (1, 'Research')) == 0


@pytest.mark.parametrize('shipped,billed,pending', [(0, '0', '40'), (2, '20', '20'), (4, '40', '0')])
def test_documented_shipment_uses_stored_unshipped_value(shipped, billed, pending):
    import receipt_review as review
    data = pasternack_receipt()
    key = (1, 'Research')
    data['groups'] = {key: [(D(40), 'Precision cable', 4, D(10)),
                           (D(5), 'Unmatched adapter', 1, D(5))]}
    data['pages'] = data['pages'][:1]
    data['text_geometry'] = [dict(ocr=[
        dict(text=t, line=(x,y,x+70,y), height=10, **extra) for t,x,y,extra in [
            ('Unit',300,730,{}), ('Amount',450,730,{}), ('Precision cable',40,700,{}),
            (str(shipped),220,700,{'column':'shipped'}), ('$10.00',300,700,{}),
            ('$'+billed+'.00',450,700,{})]])]
    markup.reconcile(data)
    supported = review.amount_sum(data['receipt_amounts'].values())
    awaiting = review.awaiting_shipment(data, key)
    assert supported == D(billed)
    assert awaiting == D(pending)
    discrepancy = supported + awaiting - D(45)
    assert discrepancy == -5
    assert D(45) == supported + awaiting - discrepancy
    assert data['receipt_amounts'][key, 1] is None


def test_both_observed_timing_runs_and_layout_calibration(monkeypatch):
    runs = json.loads((Path(__file__).parent / 'fixtures' / 'receipt_timing_calibration.json').read_text())
    now = [0.]
    monkeypatch.setattr(reports, 'monotonic', lambda: now[0])
    for run in runs:
        progress = reports.ReportProgress([{}] * run['counts']['purchases'])
        initial = progress.start('purchases')['remaining_seconds']
        assert abs(initial / run['actual_seconds'] - 1) < .2
        # Replay actual work and counts without sleeping. Live estimates must
        # adopt measured stage rates and keep the calibrated layout workload.
        for stage in run['stages']:
            name = stage['stage']
            if name == 'layout':
                # The logs recorded the old workload model before its scale.
                progress.rates[name] = stage['predicted_seconds'] * 11.24
            event = progress.start(name, stage['total'])
            if name == 'layout':
                assert progress.stage_timings[-1]['predicted_seconds'] == pytest.approx(stage['predicted_seconds'] * 11.24)
            for _ in range(stage['total']):
                now[0] += stage['actual_seconds'] / stage['total']
                progress.advance()
            assert progress.rates[name] == pytest.approx(stage['actual_seconds'] / stage['total'])
        assert progress.timing_log()['actual_seconds'] == pytest.approx(run['actual_seconds'], abs=.2)
    assert reports.ReportProgress.layout_seconds(40, 18, 45) == pytest.approx(11.24 * (.5 + .8 + .9 + 1.35))


@pytest.mark.parametrize('grouping', ['receipt', 'project'])
def test_project_summary_balance_and_overall_aggregation(monkeypatch, grouping):
    data = pasternack_receipt()
    a, b = (1, 'Alpha'), (2, 'Beta')
    data['groups'] = {a: [(D(10), 'Precision cable', 1, D(10)),
                          (D(20), 'Pending board', 2, D(10)),
                          (D(5), 'Unmatched adapter', 1, D(5))],
                      b: [(D(12), 'Price mismatch', 1, D(12)),
                          (D(40), 'Partial shipment', 4, D(10))]}
    spans = [dict(text=t, line=(x,750,x+60,750), height=10) for t,x in [('Unit',300),('Amount',450)]]
    for y, name, qty, unit, amount in [(700,'Precision cable',1,'10.00','10.00'),
                                      (650,'Pending board',0,'10.00','0.00'),
                                      (600,'Price mismatch',1,'12.10','12.10'),
                                      (550,'Partial shipment',2,'10.00','20.00')]:
        spans.extend(dict(text=t, line=(x,y,x+70,y), height=10, **extra) for t,x,extra in
                     [(name,40,{}),(str(qty),220,{'column':'shipped'}),('$'+unit,300,{}),('$'+amount,450,{})])
    spans.append(dict(text='Total $42.10', line=(40,500,250,500), height=10))
    data['pages'] = data['pages'][:1]
    data['text_geometry'] = [dict(ocr=spans)]
    second = pasternack_receipt()
    second.update(id=6, groups={a: [(D(7), 'Second cable', 1, D(7))]}, pages=second['pages'][:1],
                  text_geometry=[dict(ocr=[dict(text='Second cable $8.00', line=(40,700,250,700), height=10)])])
    tables = []
    original = reports.Table
    def capture(rows, *args, **kwargs):
        if len(rows[0]) == 5:
            tables.append([[getattr(cell, 'text', cell) for cell in row] for row in rows])
        return original(rows, *args, **kwargs)
    monkeypatch.setattr(reports, 'Table', capture)
    reports.build_pdf([data, second], grouping)
    assert tables[0][-1] == ['OVERALL TOTAL:', '94.00', '50.10', '40.00', '-3.90']
    for table in tables:
        for row in table[1:]:
            db, supported, awaiting, discrepancy = map(lambda value: D(value.replace(',', '')), row[1:])
            assert db == supported + awaiting - discrepancy
    if grouping == 'project':
        assert tables[0][1] == ['Alpha', '42.00', '18.00', '20.00', '-4.00']
        assert tables[0][2] == ['Beta', '52.00', '32.10', '20.00', '0.10']
        assert tables[1][-1] == ['PROJECT TOTAL:', '42.00', '18.00', '20.00', '-4.00']
        assert tables[2][-1] == ['PROJECT TOTAL:', '52.00', '32.10', '20.00', '0.10']
    else:
        assert tables[1][1] == ['Alpha', '35.00', '10.00', '20.00', '-5.00']
        assert tables[1][2] == ['Beta', '52.00', '32.10', '20.00', '0.10']
        assert tables[2][1] == ['Alpha', '7.00', '8.00', '0.00', '1.00']


def test_shipping_without_supported_purchase_values_stays_unresolved():
    import receipt_review as review
    data = pasternack_receipt()
    key = (1, 'Research')
    data['groups'][key] = [(D(400), 'Unmatched adapter', 1, D(400))]
    markup.reconcile(data)
    assert data['receipt_amounts'][key, 0] is None
    assert data['receipt_amounts'][key, 1] is None
    assert review.amount_sum(data['receipt_amounts'].values()) == 0
    assert review.awaiting_shipment(data, key) == 0


def test_zero_database_price_can_support_receipt_only_shipping():
    data = pasternack_receipt()
    key = (1, 'Research')
    data['groups'][key] = [(D(0), 'PE83CR006', 1, D(0))]
    markup.reconcile(data)
    assert data['receipt_amounts'][key, 0] == D('401.99')
    assert data['receipt_amounts'][key, 1] == D('9.99')
    assert sum(row[0] for row in data['groups'][key]) == 0
