import pandas as pd
import json

df = pd.read_excel('EXPORT_20260216_100124.XLSX', dtype=str)
df.columns = [c.strip() for c in df.columns]
print("Columns:", list(df.columns))
print()

# Search for Stebro customer
print("--- Searching for 4020032023 (Stebro Sold-To) ---")
for col in df.columns:
    hits = df[df[col].str.contains('4020032023', na=False)]
    if not hits.empty:
        print(f"  Found in column '{col}':")
        print(hits.to_string())
        break
else:
    print("  NOT FOUND - Stebro is not in Excel mapping!")

print()

# Search Ecoform
print("--- Searching for Ecoform ---")
hits2 = df[df.apply(lambda r: 'ecoform' in str(r.values).lower(), axis=1)]
if not hits2.empty:
    print(hits2.to_string())
else:
    print("  Ecoform NOT in Excel!")

print()

# Check enriched data for Ecoform PO 4674421 - what cust_id and material is it trying to match?
data = json.load(open('final_output_fixed.json', encoding='utf-8'))
for r in data:
    h = r.get('header_fields') or {}
    if '4674421' in str(h.get('po_number', '')):
        print(f"--- Ecoform PO 4674421 ---")
        print(f"  sold_to_id: {h.get('sold_to_id')!r}")
        print(f"  customer_number: {h.get('customer_number')!r}")
        print(f"  vendor_name: {h.get('vendor_name')!r}")
        print(f"  so_grouping_rule: {h.get('so_grouping_rule')!r}")
        for item in r.get('line_items', []):
            print(f"  Item: mat={item.get('material_code')!r} qty={item.get('quantity')} unit={item.get('unit')!r}")
