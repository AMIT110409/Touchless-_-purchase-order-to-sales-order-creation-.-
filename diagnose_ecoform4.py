import pandas as pd, json

# Load the material mapping sheet
df = pd.read_excel('EXPORT_20260216_100124.XLSX', dtype=str)
df.columns = [c.strip() for c in df.columns]

# Find all rows associated with Ecoform customer
print("=== All Excel rows for Ecoform (customer 4020000777) ===")
found = False
for _, row in df.iterrows():
    vals = [str(v) for v in row.values]
    if any('4020000777' in v or '777' in v for v in vals if v and v != 'nan'):
        print(dict(zip(df.columns, vals)))
        found = True
if not found:
    print("NOT FOUND - Ecoform is not in the MATERIAL MAPPING part of Excel")
    print("\nLet's see what columns Excel has:")
    print("Columns:", list(df.columns))
    print("\nFirst 5 rows:")
    print(df.head(5).to_string())

# Separately: Check enriched data - what qty was extracted?
print("\n=== Ecoform PO 4674421 - raw OCR context ===")
data = json.load(open('final_output_fixed.json', encoding='utf-8'))
for r in data:
    h = r.get('header_fields') or {}
    src = r.get('source_file', '')
    if 'ecoform' in src.lower():
        raw = r.get('raw_text', '')
        if raw:
            # Find lines with 8677 or quantity-like patterns
            lines = raw.split('\n')
            qty_lines = [l for l in lines if '8677' in l or 'kg' in l.lower() or 'qty' in l.lower() or 'stueck' in l.lower()]
            print(f"Source: {src}")
            print("Relevant OCR lines:")
            for l in qty_lines[:20]:
                print(f"  {repr(l)}")
        else:
            print(f"Source: {src} — NO raw_text available")
