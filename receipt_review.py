"""Evidence-backed suggestions for review; never modify stored purchase values."""
from decimal import Decimal, ROUND_HALF_UP
import re

import receipt_markup as markup

CENT = Decimal('.01')


def item_evidence(receipt):
    """Associate product rows, billed extensions and item-specific tariffs.

    A description match must identify exactly one database item. A table's
    Shipped/Unit Price/Amount columns supply stronger evidence than a unit price
    alone, especially for backordered goods. Keep zero extensions as context,
    without adding zero annotations to the report's monetary occurrence list.
    """
    items = [(key, i, item) for key, rows in receipt.get('source_groups', receipt['groups']).items()
             for i, item in enumerate(rows)]
    result = {}
    for page_index, geometry in enumerate(receipt['text_geometry']):
        spans = geometry.get('ocr', [])
        prices = markup._prices(spans)
        selections = [markup._description_matches(spans, item[1]) for _, _, item in items]
        product_lines = set().union(*selections) if selections else set()
        roles = [markup.amount_role(dict(price, spans=spans, context='')) for price in prices]
        candidates = {}
        for (key, index, item), selected in zip(items, selections):
            for p in markup._row_prices(spans, selected, product_lines, prices):
                if roles[p] == 'item':
                    candidates.setdefault(p, []).append((key, index))
        unit = next((s for s in spans if s['text'].casefold() == 'unit'), None)
        amount = next((s for s in spans if s['text'].casefold() == 'amount'), None)
        table = unit is not None and amount is not None and unit['line'][0] < amount['line'][0]
        for item_position, (key, index, item) in enumerate(items):
            ref = (key, index)
            own = [i for i, refs in candidates.items() if refs == [ref]]
            if not own:
                continue
            row = [prices[i] for i in own]
            extension = max(row, key=lambda p: p['line'][0])
            y = extension['line'][1]
            shipped = next((Decimal(s['text']) for s in spans
                            if s.get('column') == 'shipped' and abs(s['line'][1] - y) < 3), None)
            lower = max((s['line'][1] for i, s in enumerate(spans)
                         if i in product_lines - selections[item_position]
                         and s['line'][1] < y - 5), default=-float('inf'))
            # Unmatched products also bound the block; their tariffs must not be
            # assigned to a preceding purchase simply because it is in the DB.
            if table:
                lower = max(lower, max((p['line'][1] for p, role in zip(prices, roles)
                                        if role == 'item' and p['line'][1] < y - 5
                                        and unit['line'][0] - 8 <= p['line'][0] < amount['line'][0] - 8),
                                       default=-float('inf')))
            tariff_prices = [p for p in receipt['verification'][page_index]
                             if table and p['role'] == 'tariff' and lower < p['line'][1] <= y]
            tariff = sum((p['value'] for p in tariff_prices), Decimal(0))
            # An amount at the right of an explicit Amount column is a line
            # extension. Otherwise use an explicit QTY near the product card.
            explicit_extension = table and extension['line'][0] >= amount['line'][0] - 4
            if shipped is None:
                near = [s for s in spans if lower < s['line'][1] <= y + 15
                        and y - s['line'][1] < s.get('height', 10) * 14]
                text = ' '.join(s['text'] for s in near)
                quantities = re.findall(r'(?:qty|quantity)\s*:?\s*(\d+)\b', text, re.I)
                if len(set(quantities)) == 1:
                    shipped = Decimal(quantities[0])
            if shipped is None and table and explicit_extension:
                units = [p['value'] for p in row if p['line'][0] < amount['line'][0] - 8 and p['value'] > 0]
                if len(units) == 1 and extension['value'] >= 0:
                    inferred = extension['value'] / units[0]
                    if inferred == inferred.to_integral_value():
                        shipped = inferred
            if shipped is None and item[2] == 1 and not explicit_extension:
                shipped = Decimal(1)
            value = extension['value']
            # A lone unit-price row marked QTY can supply its billed extension.
            if not explicit_extension and shipped and len(row) == 1 and value == item[3]:
                value = (value * shipped).quantize(CENT, rounding=ROUND_HALF_UP)
            ids = [p['id'] for p in receipt['verification'][page_index]
                   if any(p['line'] == raw['line'] for raw in row)]
            ids.extend(p['id'] for p in tariff_prices)
            record = dict(ref=ref, item=item, page=page_index, refs=ids,
                          quantity=shipped, billed=value, tariff=tariff,
                          landed=value + tariff, explicit_extension=explicit_extension,
                          confidence=min((p['confidence'] for p in row + tariff_prices), default=0))
            # Multiple independent source rows for one DB item are ambiguous;
            # keep them visible, but do not suggest silently replacing its cost.
            if ref in result:
                result[ref]['ambiguous'] = True
            else:
                result[ref] = record
    return result


def evidence_for_price(receipt, price):
    rows = [row for row in receipt.get('item_evidence', {}).values()
            if row['page'] == price['page'] and price['id'] in row['refs']
            and not row.get('ambiguous')]
    return rows[0] if len(rows) == 1 else None


def recommendations(receipt):
    suggestions = []
    assigned_tariff = Decimal(0)
    for row in receipt['item_evidence'].values():
        key, index = row['ref']
        item = row['item']
        common = dict(item=item[1], project=key[1], refs=', '.join(row['refs']),
                      current_unit=item[3], current_total=item[0], quantity=item[2],
                      tariff=row['tariff'], proposed_unit=None, proposed_total=None)
        if row.get('ambiguous') or row['confidence'] < 60:
            continue
        assigned_tariff += row['tariff']
        if row['explicit_extension'] and row['billed'] == 0 and item[0] != 0:
            suggestions.append(dict(common, kind='unbilled', reason=(
                'Zero shipped; $0.00 billed. Check another invoice or leave this purchase pending.'
                if row['quantity'] == 0 else '$0.00 billed. Check fulfillment and the invoice assignment.')))
            continue
        if row['quantity'] is not None and row['quantity'] != item[2]:
            suggestions.append(dict(common, kind='quantity', proposed_total=row['landed'], reason=(
                f"Receipt quantity {row['quantity']}; database quantity {item[2]}. Verify quantity before changing price.")))
            continue
        if row['landed'] == item[0]:
            continue
        # Name/location evidence is required; numerical proximity alone never
        # supplies an item match. A tariff is relevant even if the gap is large.
        if not row['quantity'] or (not row['tariff'] and
                abs(row['landed'] - item[0]) > max(Decimal(1), abs(item[0]) * Decimal('.25'))):
            continue
        places = max(5, len(str(abs(int(row['quantity'])))) + 2)
        proposed = (row['landed'] / row['quantity']).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
        reason = (f"Receipt line ${row['billed']:,.2f} + tariff ${row['tariff']:,.2f} "
                  f"= ${row['landed']:,.2f}; include tariff in item cost." if row['tariff'] else
                  f"Receipt line ${row['billed']:,.2f}; verify the price against the original.")
        suggestions.append(dict(common, kind='tariff' if row['tariff'] else 'price',
                                proposed_unit=proposed, proposed_total=row['landed'], reason=reason))
    detected = sum((value for _, kind, value in receipt['charges'] if kind == 'tariff'), Decimal(0))
    separately_entered = sum((price['value'] for page in receipt['verification'] for price in page
                              if price['role'] == 'tariff'
                              and not evidence_for_price(receipt, price)
                              and markup._item_correspondence(receipt, price)), Decimal(0))
    unassigned = detected - assigned_tariff - separately_entered
    if unassigned > 0:
        suggestions.append(dict(item='Unassigned tariff', project='Invoice', refs='', kind='tariff',
                                current_unit=None, current_total=None, proposed_unit=None,
                                proposed_total=unassigned, quantity=None, tariff=unassigned,
                                reason=f"${unassigned:,.2f} tariff could not be assigned confidently; check whether already included in item costs."))
    return suggestions
