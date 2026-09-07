import json, re, pandas as pd

data = json.load(open('final_output_fixed.json', encoding='utf-8'))
df = pd.read_excel('EXPORT_20260216_100124.XLSX', dtype=str)
df.columns = [c.strip() for c in df.columns]

# Find the Ecoform record(s)
print("=== Ecoform PO 4674421 details ===")
for r in data:
    h = r.get('header_fields') or {}
    po = str(h.get('po_number') or '')
    vendor = str(h.get('vendor_name') or '')
    src = r.get('source_file', '')
    if '4674421' in po or 'ecoform' in vendor.lower() or 'ecoform' in src.lower():
        print(f"Source file: {src}")
        print(f"PO Number: {po}")
        print(f"Vendor: {vendor}")
        print(f"sold_to_id: {repr(h.get('sold_to_id'))}")
        print(f"customer_number: {repr(h.get('customer_number'))}")
        print(f"so_grouping_rule: {repr(h.get('so_grouping_rule'))}")
        m = re.search(r'Sold-to[\s_-]+(\d+)', src, re.IGNORECASE)
        print(f"Filename Sold-to match: {repr(m.group(1) if m else None)}")
        for item in r.get('line_items', []):
            print(f"  Item: material={repr(item.get('material_code'))} qty={item.get('quantity')} unit={repr(item.get('unit'))}")
        print()

# Check what Excel has for Ecoform / similar IDs
print("=== Excel - searching for Ecoform-related IDs ===")
cust_col = df.columns[1]
for _, row in df.iterrows():
    vals = list(row.values)
    row_str = str(vals).lower()
    if 'ecoform' in row_str or '4020001' in row_str or 'multiform' in row_str:
        print(dict(zip(df.columns, vals)))
