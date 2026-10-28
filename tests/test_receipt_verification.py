"""Receipt discrepancies transcribed from the supplied reports, plus coverage invariants."""
from decimal import Decimal as D
from io import BytesIO

import pytest
from pypdf import PdfReader
from reportlab.pdfgen import canvas

import receipt_markup as markup
import receipt_reports as reports


@pytest.fixture(autouse=True)
def synthetic_ocr(monkeypatch):
    # Reconciliation tests supply deterministic recognized text. Backend and
    # corrupt-text-layer behavior are covered in test_receipt_ocr.py.
    monkeypatch.setattr(markup, 'ocr_spans', lambda page: markup.text_spans(page))


def receipt(lines, items, number=1):
    buf = BytesIO()
    pdf = canvas.Canvas(buf, pagesize=(612, 792))
    for page in lines:
        for i, line in enumerate(page):
            pdf.drawString(40, 740 - i * 22, line)
        pdf.showPage()
    pdf.save()
    return {'id': number, 'groups': {(1, 'Research'): items},
            'pages': reports.document_pages(buf.getvalue())}


def item(name, quantity, cost):
    return (D(cost) * quantity, name, quantity, D(cost))


@pytest.mark.parametrize('number,lines,items,total,difference', [
    (11, [['Garolite rod 12.88 25.76', 'Snap grip clamps 6.00 12.00',
           'Tube fitting 6.12 18.36', 'Merchandise 56.12', 'Shipping 44.90', 'Total $101.02']],
     [item('Garolite rod', 2, '12.88'), item('Snap grip clamps', 2, '6.00'), item('Tube fitting', 3, '5.67')], '101.02', '1.35'),
    (12, [['Stainless steel Adjustable clamp $28.34', '$2.18 /ea. QTY 13',
           'FIP Adapter Fitting $6.44', 'Garden hose Adapter Fitting $35.30',
           '$7.06 /ea. QTY 5', 'Subtotal $70.08', 'Total Billed $70.08']],
     [item('Stainless steel Adjustable clamp', 15, '2.18'), item('FIP Adapter Fitting', 1, '6.44'),
      item('Garden hose Adapter Fitting', 5, '7.06')], '70.08', '-4.36'),
    (13, [['Shipping Insurance $3.75 $3.75', 'BBV100DWV Dixon Valve $154.39 $154.39',
           'SUB TOTAL $158.14', 'SHIPPING $16.09', 'TAX $0.00', 'TOTAL $174.23']],
     [item('BBV100DWV Dixon Valve', 1, '154.39')], '174.23', '0.00'),
    (17, [['Philips Rechargeable Lithium AA Batteries $35.99', 'Salfboy Clear Safety Glasses $34.19',
           'Item(s) Subtotal $184.89', 'Shipping & Handling $0.00', 'Your Coupon Savings -$7.00',
           'Total before tax $177.89', 'Estimated tax to be collected $0.00', 'Grand Total $177.89'],
          ['WALI Desk File Organizer $16.99', 'Fireproof Accordion File Organizer $21.59',
           'YSAGi Large Extended Desk Pad Protector $15.99', 'Item(s) Subtotal $54.57',
           'Shipping & Handling $0.00', 'Grand Total $54.57']],
     [item('Philips Rechargeable Lithium AA Batteries', 1, '39.99'),
      item('Salfboy Clear Safety Glasses', 1, '4.78'),
      item('Fireproof Accordion File Organizer', 1, '22.79'),
      item('YSAGi Leather Office Desk Pad Protector', 1, '14.39'),
      item('Other purchases', 1, '129.90')], '232.46', '27.61'),
    (20, [['TP-Link Gigabit Ethernet Network Switch $16.09', 'Item(s) Subtotal $208.57',
           'Shipping & Handling $0.00', 'Grand Total $208.57'],
          ['Ceramic Magnet Block $32.99', 'Item(s) Subtotal $65.98', 'Grand Total $65.98']],
     [item('TP-Link Gigabit Ethernet Network Switch', 1, '16.99'), item('Ceramic Magnet Block', 2, '32.99'),
      item('Other purchases', 1, '192.48')], '274.55', '-0.90'),
])
def test_known_receipt_discrepancies(number, lines, items, total, difference):
    data = receipt(lines, items, number)
    # Charges must be entered, rather than silently added by OCR.
    if number == 11:
        data['shipping'] = D('44.90')
    elif number == 13:
        data['shipping'] = D('16.09')
        data['groups'][1, 'Research'].append(item('Shipping Insurance', 1, '3.75'))
    elif number == 17:
        difference = '20.61'  # The coupon has not been entered in the database.
    data = markup.reconcile(data)
    assert data['original_total'] == D(total)
    assert data['unresolved'] == D(difference)
    annotations = markup.verify_amounts(data)
    for prices, decisions in zip(data['verification'], annotations):
        assert [p['id'] for p in prices] == [p['id'] for p in decisions]
        assert all(p['reason'] for p in decisions)
    if number != 13:
        assert any(p['status'] == '?' for page in annotations for p in page)
        assert all(p['status'] == '?' for page in annotations for p in page if p['role'] == 'total')
    else:
        assert data['allocated_total'] == D('174.23')
        assert data['groups'][1, 'Research'][-1][0] == D('16.09')
        assert all(p['status'] == 'ok' for page in annotations for p in page if p['role'] == 'total')


def test_proration_tax_exclusion_discounts_and_exact_cent_reconciliation():
    data = receipt([['Precision cable 10.00', 'Shielded adapter 20.00',
                     'Shipping $1.01', 'Insurance $0.05', 'Discount -$0.02',
                     'Tariff $0.03', 'Sales tax $2.40', 'Grand Total $33.47']],
                   [item('Precision cable', 1, '10.00')])
    data['groups'][2, 'Office'] = [item('Shielded adapter', 1, '20.00')]
    data['groups'][1, 'Research'].extend([
        item('Insurance charge', 1, '.05'), item('Discount adjustment', 1, '-.02'), item('Tariff charge', 1, '.03')])
    data['shipping'] = D('1.01')
    data = markup.reconcile(data)
    assert data['original_total'] == D('33.47')
    assert data['reimbursable_total'] == data['allocated_total'] == D('31.07')
    assert data['unresolved'] == 0
    assert [rows[-1][0] for rows in data['groups'].values()] == [D('.34'), D('.67')]
    decisions = markup.verify_amounts(data, (1, 'Research'))[0]
    assert next(p for p in decisions if p['role'] == 'tax')['status'] == 'strike'
    assert next(p for p in decisions if p['value'] == 20)['status'] == 'strike'
    assert next(p for p in decisions if p['role'] == 'total')['status'] == 'ok'
    assert next(p for p in decisions if p['value'] == 10)['status'] == 'ok'


def test_same_number_without_context_never_verifies_and_every_occurrence_is_marked():
    data = markup.reconcile(receipt([['Precision cable $10.00', 'Unrelated price 10.00',
                                     'Something else $10', 'Sales Tax $1.00', 'Total $11.00']],
                                    [item('Precision cable', 1, '10.00')]))
    decisions = markup.verify_amounts(data)[0]
    assert len(decisions) == 5
    assert [p['status'] for p in decisions[:3]] == ['ok', '?', '?']
    assert len({p['id'] for p in decisions}) == 5


def test_source_quantity_disagreement_prevents_green_unit_price():
    data = markup.reconcile(receipt([['Precision cable QTY 13 $2.18 $28.34', 'Total $28.34']],
                                    [item('Precision cable', 15, '2.18')]))
    assert all(p['status'] == '?' for p in markup.verify_amounts(data)[0])


def test_native_prices_are_not_added_to_ocr_evidence(monkeypatch):
    data = receipt([['Precision cable $10.00', 'Total $10.00']], [item('Precision cable', 1, '10.00')])
    monkeypatch.setattr(markup, 'text_spans', lambda page: [])
    monkeypatch.setattr(markup, 'ocr_spans', lambda page: [
        {'text': '$10.00', 'line': (40, 600, 80, 600)}])
    data = markup.reconcile(data)
    decisions = markup.verify_amounts(data)[0]
    assert len(decisions) == 1
    assert decisions[0]['status'] == '?'
    output = reports.build_pdf([data], 'receipt')
    text = '\n'.join(p.extract_text() for p in PdfReader(BytesIO(output)).pages)
    assert 'Receipt errors' in text
    assert 'No matching database item was found' in ' '.join(text.split())
    assert 'Precision cable $10.00' in text


@pytest.mark.parametrize('grouping', ['receipt', 'project'])
def test_report_has_plain_receipt_comparison_and_audit_trail(grouping):
    data = receipt([['Precision cable $10.00', 'Shipping $1.00', 'Sales tax $0.80', 'Total $11.80']],
                   [item('Precision cable', 1, '10.00')])
    data['shipping'] = D('1.00')
    original = data['pages'][0].get_contents().get_data()
    output = reports.build_pdf([data], grouping)
    text = '\n'.join(p.extract_text() for p in PdfReader(BytesIO(output)).pages)
    for label in ('Shipping', 'Line total', 'original receipt total',
                  'sales tax excluded', 'p1.1'):
        assert label in text
    assert 'Receipt reconciliation' not in text
    assert 'ALLOCATED TOTAL' not in text
    assert data['pages'][0].get_contents().get_data() == original


def test_whole_currency_amounts_and_labeled_amounts_without_currency():
    data = markup.reconcile(receipt([['Precision cable $10', 'Shipping 2', 'Tax: 1', 'Total 13']],
                                    [item('Precision cable', 1, '10')]))
    assert data['original_total'] == 13
    assert data['reimbursable_total'] == 12
    assert data['excluded_tax'] == 1
    assert len(markup.verify_amounts(data)[0]) == 4


def test_final_regex_pass_detects_a_removed_occurrence_even_with_same_value():
    data = markup.reconcile(receipt([['Precision cable $10.00', 'Total $10.00']],
                                    [item('Precision cable', 1, '10')]))
    data['verification'][0].pop()
    decisions = markup.verify_amounts(data)[0]
    assert len(decisions) == 2
    assert decisions[-1]['status'] == '?'
    assert 'Final coverage check' in decisions[-1]['reason']


def test_insurance_unit_and_extension_are_not_both_allocated():
    data = markup.reconcile(receipt([['Precision cable $10.00', 'Insurance QTY 2 $3.75 $7.50',
                                     'Total $17.50']], [item('Precision cable', 1, '10')]))
    assert data['allocated_total'] == D('10.00')
    assert data['unresolved'] == D('7.50')
    assert data['charges'] == [('p1.3', 'insurance', D('7.50'))]


def test_invoice_shipping_prorates_once_across_projects_and_conserves_cents():
    data = receipt([['Precision cable $10.00', 'Shielded adapter $20.00',
                     'Shipping $1.01', 'Total $31.01']], [item('Precision cable', 1, '10')])
    data['groups'][2, 'Office'] = [item('Shielded adapter', 1, '20')]
    data['shipping'] = D('1.01')
    markup.reconcile(data)
    assert data['allocated_total'] == D('31.01')
    assert [sum(row[0] for row in rows) for rows in data['groups'].values()] == [D('10.34'), D('20.67')]
    assert data['source_groups'][1, 'Research'] == [item('Precision cable', 1, '10')]
    markup.reconcile(data)
    assert data['allocated_total'] == D('31.01')
    assert data['unresolved'] == 0
    assert len(data['groups'][1, 'Research']) == 2


def test_total_only_purchase_never_has_ocr_shipping_added_again():
    data = receipt([['Workstation components $9401.31', 'Shipping-Including insurance and handling 1.00 Units 175.00 0% $175.00'],
                    ['Untaxed Amount $9576.31', 'Total $9576.31']],
                   [item('Complete workstation', 1, '9576.31')], 10)
    data['explanation'] = 'Workstation entered as one total including shipping.'
    markup.reconcile(data)
    assert data['allocated_total'] == D('9576.31')
    assert data['unresolved'] == 0
    decisions = markup.verify_amounts(data)
    assert next(p for page in decisions for p in page if p['role'] == 'total')['status'] == 'ok'
    assert all(p['value'] != 1 for page in decisions for p in page)
    output = reports.build_pdf([data], 'project')
    text = ' '.join(p.extract_text() for p in PdfReader(BytesIO(output)).pages)
    assert data['explanation'] in text
    assert 'OCR amount $9,401.31' in text
    assert 'See invoice explanation above' in text


def test_sigatek_model_ocr_subtotal_and_carrier_shipping():
    data = receipt([['$C1030120 Directional Coupler 10 dB $255.00', 'Sub-Total: $255.00',
                     'FedEx (1 Boxes) (Fedex Ground): $14.88', 'Total: $269.88']],
                   [item('SC1030120', 1, '255')], 6)
    data['shipping'] = D('14.88')
    markup.reconcile(data)
    decisions = markup.verify_amounts(data)[0]
    assert [p['role'] for p in decisions] == ['item', 'subtotal', 'shipping', 'total']
    assert all(p['status'] == 'ok' for p in decisions)
    assert data['unresolved'] == 0


@pytest.mark.parametrize('grouping', ['receipt', 'project'])
def test_unreadable_support_still_generates_pdf_with_review_notice(monkeypatch, grouping):
    data = receipt([['Precision cable $10.00', 'Total $10.00']], [item('Precision cable', 1, '10')])
    monkeypatch.setattr(markup, 'text_spans', lambda page: [])
    monkeypatch.setattr(markup, 'ocr_spans', lambda page: [])
    output = reports.build_pdf([data], grouping)
    text = '\n'.join(p.extract_text() for p in PdfReader(BytesIO(output)).pages)
    assert 'OCR could not read this receipt' in text
    assert 'Precision cable $10.00' in text
    assert 'no matching receipt item was found' in text


def test_text_extraction_failure_is_reported_inside_pdf(monkeypatch):
    data = receipt([['Precision cable $10.00']], [item('Precision cable', 1, '10')])
    def unreadable(*args, **kwargs):
        raise ValueError('Unsupported text encoding')
    monkeypatch.setattr(data['pages'][0], 'extract_text', unreadable)
    monkeypatch.setattr(markup, 'ocr_spans', lambda page: [])
    output = reports.build_pdf([data], 'receipt')
    text = '\n'.join(p.extract_text() for p in PdfReader(BytesIO(output)).pages)
    assert 'OCR could not read this receipt' in text
    assert 'Precision cable $10.00' in text
