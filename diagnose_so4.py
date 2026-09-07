import json, re, pandas as pd

data = json.load(open('final_output_fixed.json', encoding='utf-8'))
df = pd.read_excel('EXPORT_20260216_100124.XLSX', dtype=str)
df.columns = [c.strip() for c in df.columns]

# Print all Excel rows to see what customer IDs are in there
print("=== Full Excel Data (Customer + SO Rule) ===")
cust_col = df.columns[1]
so_rule_col = [c for c in df.columns if 'SO' in c.upper() or 'separ' in c.lower() or 'one' in c.lower()]
print(f"Customer col: {cust_col!r}, SO Rule cols: {so_rule_col}")
for _, row in df.iterrows():
    vals = {c: row[c] for c in df.columns}
    print(dict(vals))
print()

# List all source files in enriched data
print("=== All source files in enriched output ===")
for r in data:
    h = r.get('header_fields') or {}
    print(f"  {r['source_file']} | sold_to={h.get('sold_to_id')!r} | so_rule={h.get('so_grouping_rule')!r}")
