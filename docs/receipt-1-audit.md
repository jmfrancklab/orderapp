# Receipt #1 review

Reviewed `receipts-by-project.pdf` supplied October 28, 2026, including the
rendered DigiKey invoice dated August 14, 2026. Database values below come from
the purchase tables in that report; the local `orders.db` does not contain
invoice #1. This review does not modify purchase records.

The invoice is a partial shipment. Its arithmetic is correct:

| Component | Amount |
| --- | ---: |
| Shipped merchandise | $1,426.94 |
| Item-specific tariffs | $127.39 |
| Shipping | $8.49 |
| Total charged | **$1,562.82** |
| Purchases and shipping recorded in the report | $1,917.73 |
| Database excess for this invoice | **$354.91** |

The excess is $476.72 of unshipped goods plus $5.58 of other price differences,
less $127.39 of tariffs omitted from item costs. Both ADS127L21 ($394.00 in
the database) and the Connor-Winfield oscillator ($82.72) have **zero shipped
and $0.00 billed** on this invoice. They may have another shipment/invoice;
verify their invoice assignment rather than changing their unit prices to zero.

The following proposed costs include the tariff printed underneath each shipped
line. Shipping remains separate in the invoice Shipping field.

| Item | Quantity | Stored unit cost → proposed unit cost | Receipt merchandise + tariff | Proposed line total |
| --- | ---: | --- | --- | ---: |
| AD7902 | 1 | $140.00 → $151.05 | $139.86 + $11.19 | $151.05 |
| EVAL-ADICUP3029 | 1 | $65.00 → $83.10 | $64.92 + $18.18 | $83.10 |
| AD5941 | 1 | $416.00 → $449.52 | $416.22 + $33.30 | $449.52 |
| AH49HZ3-G1 | 3 | $1.04 → $0.64667 | $1.56 + $0.38 | $1.94 |
| AD8420ARMZ-R7 | 2 | $3.64 → $3.93 | $7.28 + $0.58 | $7.86 |
| ADF4372 | 1 | $757.86 → $818.49 | $757.86 + $60.63 | $818.49 |
| TLV333 | 2 | $1.03 → $1.11 | $2.06 + $0.16 | $2.22 |
| DS18B20+PAR | 5 | $8.24 → $8.03 | $37.18 + $2.97 | $40.15 |
| **Total** | | | | **$1,554.33** |

With the two unshipped purchases assigned appropriately and these prices
verified and entered, adding $8.49 shipping yields $1,562.82 exactly. The
Hall-sensor unit cost needs sub-cent precision; rounding it to $0.65 would
produce $1.95 instead of the billed $1.94.

## Coding problems found

- A matching unit price incorrectly validated an unshipped purchase. OCR's
  general page-layout pass also omitted some zero Amount cells and Shipped
  counts; targeted cell OCR now recovers them.
- `Total Sales and Tariff` was classified as a tariff, and the tariff summary
  was counted again alongside detailed charges. These are separate summary
  roles, with each actual tariff counted once.
- ADICUP3029's parenthetical reference to AD5941 produced a second, ambiguous
  AD5941 match. Matching now prefers the primary product name.
- `1.64 kgs` was interpreted as money. Measurement suffixes are excluded.
- The editor rounded all unit costs to cents, preventing the correct Hall-sensor
  landed cost from being entered. Unit-cost storage/display preserves precision.
- Source PDF vectors could render differently in Chrome. Supporting pages now
  embed JPEG encodings of the same rendered bitmaps used for OCR, with image
  resources reused across project sections.

## Accounting basis

Tariffs attributable to acquiring goods form part of acquisition cost under
U.S. GAAP (ASC 330-10-30-1). Including the attributable tariff in the stored item
cost is consistent with this principle; GAAP does not prescribe this app's
specific database columns. Capitalization versus expensing of laboratory/R&D
purchases is a separate question. The report's existing sales-tax exclusion is
a reimbursement policy, not a general GAAP rule.

Sources: [KPMG, Inventory, Question 3.2.20](https://kpmg.com/kpmg-us/content/dam/kpmg/frv/pdf/2025/inventory.pdf#page=33),
[Deloitte, Navigating Tariffs](https://dart.deloitte.com/USDART/home/publications/deloitte/accounting-spotlight/2025/tariffs-accounting-financial-reporting-considerations).
