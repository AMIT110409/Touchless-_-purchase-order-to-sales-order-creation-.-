import pandas as pd, json

# Load material mapping sheet - check all sheets
xl = pd.ExcelFile('EXPORT_20260216_100124.XLSX')
print("Sheets:", xl.sheet_names)

# Load with right sheet
df = xl.parse(xl.sheet_names[0], dtype=str)
df.columns = [c.strip() for c in df.columns]
print("Columns:", list(df.columns))
print()

# Look for customer 4020000777 rows
print("=== Excel rows for customer 4020000777 ===")
for _, row in df.iterrows():
    if '4020000777' in str(list(row.values)):
        print(dict(zip(df.columns, row.values)))

# Load enriched and check what cust_id enrich_results actually uses for Ecoform
data = json.load(open('final_output_fixed.json', encoding='utf-8'))
for r in data:
    h = r.get('header_fields') or {}
    src = r.get('source_file', '')
    if 'ecoform' in src.lower():
        import re
        m = re.search(r'Sold-to[\s_-]+(\d+)', src, re.IGNORECASE)
        fn_id = m.group(1) if m else None
        cust_id = fn_id or h.get('customer_number') or h.get('sold_to_id')
        print(f"\nEcoform cust_id used for material lookup: {repr(cust_id)}")
        print(f"source_file: {src}")
        for item in r.get('line_items', []):
            print(f"  mat={repr(item.get('material_code'))} qty={item.get('quantity')} internal={repr(item.get('internal_material'))}")
