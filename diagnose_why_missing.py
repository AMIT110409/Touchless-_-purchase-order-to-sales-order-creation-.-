import pandas as pd, json, sys
sys.stdout.reconfigure(encoding='utf-8')

EXCEL   = "EXPORT_20260216_100124.XLSX"
JSON_IN = "final_output_v8.json"

# Build Excel sets
df = pd.read_excel(EXCEL, dtype=str, engine='openpyxl')
excel_cust_ids = set()
material_map   = {}

for _, row in df.iterrows():
    cid = str(row.get("Customer", "")).strip()
    if cid.endswith(".0"): cid = cid[:-2]
    if not cid or cid.lower() == "nan": continue
    excel_cust_ids.add(cid)
    cm = str(row.get("Customer Material Number", "")).strip()
    im = str(row.get("Material", "")).strip()
    if cm.endswith(".0"): cm = cm[:-2]
    if im.endswith(".0"): im = im[:-2]
    if cm and im and cm.lower() != "nan" and im.lower() != "nan":
        material_map[(cid, cm)] = im

# Load JSON
with open(JSON_IN, encoding='utf-8') as f:
    data = json.load(f)

# Categorize
cat_mapped        = []
cat_not_in_excel  = {}
cat_code_mismatch = {}
cat_no_mat_code   = []

for r in data:
    if "error" in r:
        continue
    h       = r.get("header_fields", {})
    sold_to = h.get("sold_to_id", "") or ""
    cname   = h.get("customer_id_or_name", "?")
    fname   = r.get("source_file", "")
    in_xl   = sold_to in excel_cust_ids

    for item in r.get("line_items", []):
        mc = item.get("material_code")
        im = item.get("internal_material")

        if im:
            cat_mapped.append((cname, mc, im))
        elif not mc:
            cat_no_mat_code.append((cname, fname))
        elif not in_xl:
            k = (cname, sold_to)
            cat_not_in_excel.setdefault(k, []).append(mc)
        else:
            excel_keys = sorted(set(k[1] for k in material_map if k[0] == sold_to))
            k = (cname, sold_to)
            cat_code_mismatch.setdefault(k, {"extracted": [], "excel_keys": excel_keys})
            cat_code_mismatch[k]["extracted"].append(mc)

total = len(cat_mapped) + len(cat_not_in_excel) + len(cat_code_mismatch) + len(cat_no_mat_code)

print("="*70)
print(f"TOTAL LINE ITEMS: {total}")
print("="*70)
print(f"  MAPPED (internal_material OK):      {len(cat_mapped)}")
print(f"  NO MAT CODE in PDF:                 {len(cat_no_mat_code)}")
print(f"  SOLD-TO NOT IN EXCEL MAPPING FILE:  {sum(len(v) for v in cat_not_in_excel.values())}")
print(f"  CODE MISMATCH (in Excel, no match): {sum(len(v['extracted']) for v in cat_code_mismatch.values())}")
print()

print("--- CUSTOMERS NOT IN EXCEL ---")
for (cname, sid), mats in sorted(cat_not_in_excel.items()):
    print(f"  {cname:<40} Sold-To={sid}  ({len(mats)} items)")
print()

print("--- CODE MISMATCHES (customer IS in Excel but extracted code has no match) ---")
for (cname, sid), info in sorted(cat_code_mismatch.items()):
    print(f"  {cname:<40} Sold-To={sid}")
    print(f"    Extracted from PDF : {info['extracted']}")
    print(f"    Excel expects codes: {info['excel_keys']}")
    print()

print("--- NO MATERIAL CODE EXTRACTED FROM PDF ---")
for cname, fname in cat_no_mat_code:
    print(f"  {cname:<40} {fname}")
