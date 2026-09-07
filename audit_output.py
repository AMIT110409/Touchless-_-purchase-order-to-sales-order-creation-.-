"""
Audit Script - Check all required fields in final output
"""
import json

with open("final_output_std.json", encoding="utf-8") as f:
    data = json.load(f)

# Required fields to check
REQUIRED_FIELDS = {
    "Extracted from PO": [
        "po_number", "order_date", "requested_delivery_date",
        "customer_id_or_name", "vendor_name",
        "ship_to_address", "sold_to_address"
    ],
    "Celonis KB Mapping": [
        "sold_to_id", "ship_to_id", "ship_to_name", "customer_number"
    ],
    "Excel Mapping": [
        "sales_organization", "order_type", "so_grouping_rule"
    ]
}

LINE_ITEM_FIELDS = ["material_description", "quantity", "unit", "material_code"]

# Deduplicate by source_file
seen = set()
unique_records = []
for r in data:
    sf = r.get("source_file", "")
    if sf not in seen:
        seen.add(sf)
        unique_records.append(r)

total = len(unique_records)
print(f"\n{'='*70}")
print(f"FIELD COVERAGE AUDIT  |  Total Unique PO Files: {total}")
print(f"{'='*70}")

for group, fields in REQUIRED_FIELDS.items():
    print(f"\n📋 {group}")
    print(f"  {'Field':<35} {'Present':>8} {'Missing':>8} {'Coverage':>10}")
    print(f"  {'-'*63}")
    for field in fields:
        present = sum(1 for r in unique_records if r.get("header_fields", {}).get(field))
        missing = total - present
        pct = present / total * 100
        flag = "✅" if pct >= 80 else ("⚠️ " if pct >= 40 else "❌")
        print(f"  {flag} {field:<33} {present:>8} {missing:>8} {pct:>9.0f}%")

print(f"\n📦 Line Item Fields")
print(f"  {'Field':<35} {'Present':>8} {'Missing':>8} {'Coverage':>10}")
print(f"  {'-'*63}")
for field in LINE_ITEM_FIELDS:
    records_with_items = [r for r in unique_records if r.get("line_items")]
    present = sum(1 for r in records_with_items if any(i.get(field) for i in r.get("line_items", [])))
    total_with_items = len(records_with_items)
    pct = present / total_with_items * 100 if total_with_items else 0
    flag = "✅" if pct >= 80 else ("⚠️ " if pct >= 40 else "❌")
    print(f"  {flag} {field:<33} {present:>8} {total_with_items - present:>8} {pct:>9.0f}%")

print(f"\n{'='*70}")
print("SAMPLE RECORD (first enriched):")
print(f"{'='*70}")
for r in unique_records:
    h = r.get("header_fields", {})
    if h.get("sold_to_id") and h.get("sales_organization"):
        print(f"  Source File    : {r.get('source_file')}")
        print(f"  PO Number      : {h.get('po_number')}")
        print(f"  Customer Name  : {h.get('customer_id_or_name')}")
        print(f"  Vendor Name    : {h.get('vendor_name')}")
        print(f"  Sold-To ID     : {h.get('sold_to_id')}")
        print(f"  Ship-To ID     : {h.get('ship_to_id')}")
        print(f"  Ship-To Name   : {h.get('ship_to_name')}")
        print(f"  Sales Org      : {h.get('sales_organization')}")
        print(f"  Order Type     : {h.get('order_type')}")
        print(f"  SO Rule        : {h.get('so_grouping_rule')}")
        items = r.get("line_items", [])
        if items:
            print(f"  Line Items     : {len(items)} item(s)")
            print(f"    First Item   : {items[0].get('material_description')} | Qty: {items[0].get('quantity')} {items[0].get('unit')}")
        break
print(f"{'='*70}\n")
