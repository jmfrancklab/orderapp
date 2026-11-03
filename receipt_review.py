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
        unit = next((s for s in spans if s['text'].casefold() in ('unit', 'price')), None)
        amount = next((s for s in spans if s['text'].casefold() == 'amount'), None)
        if amount is None and unit is not None:
            # McMaster labels its extension column Total, alongside Price.
            # Require the same header row so the invoice grand total cannot
            # accidentally turn a unit price into a billed extension.
            amount = next((s for s in spans if s['text'].casefold() == 'total'
                           and abs(s['line'][1] - unit['line'][1]) < 3
                           and s['line'][0] > unit['line'][0]), None)
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
            extension_column = (unit['line'][0] + amount['line'][0]) / 2 if table else None
            explicit_extension = table and extension['line'][0] >= extension_column
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
            unit_only = table and not explicit_extension
            if not table and len(row) == 1:
                context = spans[extension['owner']]['text']
                unit_only = bool(re.search(r'unit (?:price|cost)|/ea\b|each\b', context, re.I))
            if unit_only and shipped is not None:
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
    shipping = sum((value for _, kind, value in receipt['charges'] if kind == 'shipping'), Decimal(0))
    current = Decimal(str(receipt.get('shipping') or '0'))
    if (shipping != current and not receipt.get('shipping_in_combined_purchase')
            and any(kind == 'shipping' for _, kind, _ in receipt['charges'])):
        reason = (f'Shipping from this receipt has not been entered for this invoice. '
                  f'Enter ${shipping:,.2f} in the invoice Shipping field.'
                  if current == 0 and shipping > 0 else
                  f'Invoice Shipping is ${current:,.2f}; the receipt shows ${shipping:,.2f}. '
                  f'Enter ${shipping:,.2f} in the invoice Shipping field.')
        suggestions.append(dict(item='Invoice shipping', project='Invoice', kind='shipping',
                                refs=', '.join(ref for ref, kind, _ in receipt['charges'] if kind == 'shipping'),
                                current_unit=None, proposed_unit=None, current_total=current,
                                proposed_total=shipping, quantity=None, tariff=Decimal(0),
                                reason=reason))
    return suggestions


def receipt_amounts(receipt):
    """Receipt-derived line amounts; unsupported purchases contribute $0.00."""
    amounts = {}
    source = receipt.get('source_groups', receipt['groups'])
    for key, items in source.items():
        for index, item in enumerate(items):
            ref = (key, index)
            row = receipt.get('item_evidence', {}).get(ref)
            amounts[ref] = Decimal(0)
            if row and not row.get('ambiguous') and row['confidence'] >= 60:
                if row['explicit_extension'] or row['quantity'] is not None:
                    amounts[ref] = row['landed'].quantize(CENT, rounding=ROUND_HALF_UP)
    # Share the exact correspondence used by the green OCR checks. A printed
    # extension can support a purchase even without recognizable headings.
    # Explicit extensions, source quantities and ambiguous rows take priority.
    for page in receipt['verification']:
        for price in page:
            if price['role'] not in ('item', 'total', 'before_tax'):
                continue
            match = markup._item_correspondence(receipt, price)
            if match and price['confidence'] >= 60:
                key, index, item, _ = match
                row = receipt.get('item_evidence', {}).get((key, index))
                incomplete = row is None or (not row.get('ambiguous')
                                             and not row['explicit_extension']
                                             and row['quantity'] is None)
                if (not amounts[key, index] and incomplete
                        and price['value'] == item[0]):
                    amounts[key, index] = price['value']

    shipping_rows = [(key, i) for key, items in receipt['groups'].items()
                     for i, _ in enumerate(items)
                     if receipt.get('source_refs', {}).get((key, i)) == 'shipping']
    shipping_amounts = dict.fromkeys(shipping_rows, Decimal(0))
    shipping_charges = [value for _, kind, value in receipt['charges'] if kind == 'shipping']
    if shipping_rows and shipping_charges and not receipt.get("shipping_in_combined_purchase"):
        shipping = sum(shipping_charges, Decimal(0))
        bases = {key: sum((max(Decimal(0), amounts[key, i] or Decimal(0))
                           for i in range(len(items))), Decimal(0))
                 for key, items in source.items()}
        base = sum(bases.values(), Decimal(0))
        if base:
            previous, cumulative = Decimal(0), Decimal(0)
            for key, i in shipping_rows:
                cumulative += bases.get(key, Decimal(0))
                rounded = (shipping * cumulative / base).quantize(CENT, rounding=ROUND_HALF_UP)
                shipping_amounts[key, i] = rounded - previous
                previous = rounded
    displayed = {}
    for key, items in receipt['groups'].items():
        for i, _ in enumerate(items):
            ref = receipt.get('source_refs', {}).get((key, i), (key, i))
            displayed[key, i] = (shipping_amounts.get((key, i), Decimal(0)) if ref == 'shipping'
                                else amounts.get(ref, Decimal(0)))
    return displayed


def amount_sum(amounts):
    """Sum confirmed support; unresolved detail rows contribute no amount."""
    return sum((value for value in amounts if value is not None), Decimal(0))


def awaiting_shipment(receipt, key):
    """Stored value of uniquely matched, demonstrably unbilled quantities."""
    total = Decimal(0)
    for (project, _), row in receipt.get('item_evidence', {}).items():
        if project != key or row.get('ambiguous') or row['confidence'] < 60:
            continue
        amount, _, quantity, cost = row['item']
        if row['billed'] == 0:
            total += amount
        elif row['explicit_extension'] and row['quantity'] is not None and 0 <= row['quantity'] < quantity:
            total += (cost * (Decimal(quantity) - row['quantity'])).quantize(CENT, rounding=ROUND_HALF_UP)
    return total
