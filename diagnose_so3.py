import json, re, pandas as pd

data = json.load(open('final_output_fixed.json', encoding='utf-8'))
df = pd.read_excel('EXPORT_20260216_100124.XLSX', dtype=str)
df.columns = [c.strip() for c in df.columns]
print("Excel Columns:", list(df.columns))
print("Excel has", len(df), "rows\n")

# Print first col values (customer IDs)
cust_col = df.columns[1]  # Usually 'Customer' or 'Sold-to party'
print(f"Customer column: '{cust_col}'")
print("Sample customer IDs:", list(df[cust_col].head(20)))
print()

# Check Stebro records
print("--- Stebro records ---")
for r in data:
    src = r.get('source_file', '')
    raw = str(r.get('raw_text', ''))
    if 'stebro' in raw.lower() or 'stebro' in src.lower():
        h = r.get('header_fields') or {}
        m = re.search(r'Sold-to[\s_]+(\d+)', src, re.IGNORECASE)
        fn_id = m.group(1) if m else None
        print(f"  File: {src}")
        print(f"  sold_to_id={repr(h.get('sold_to_id'))} cust_num={repr(h.get('customer_number'))} filename_id={repr(fn_id)}")
        print(f"  so_rule={repr(h.get('so_grouping_rule'))} sales_org={repr(h.get('sales_organization'))}")

print()

# Check Ecoform PO 4674421
print("--- Ecoform PO 4674421 ---")
for r in data:
    h = r.get('header_fields') or {}
    if '4674421' in str(h.get('po_number', '')):
        src = r.get('source_file', '')
        m = re.search(r'Sold-to[\s_]+(\d+)', src, re.IGNORECASE)
        fn_id = m.group(1) if m else None
        print(f"  File: {src}")
        print(f"  sold_to_id={repr(h.get('sold_to_id'))} cust_num={repr(h.get('customer_number'))} filename_id={repr(fn_id)}")
        print(f"  so_rule={repr(h.get('so_grouping_rule'))}")
        for item in r.get('line_items', []):
            print(f"  Item: mat={repr(item.get('material_code'))} qty={item.get('quantity')} unit={repr(item.get('unit'))}")
