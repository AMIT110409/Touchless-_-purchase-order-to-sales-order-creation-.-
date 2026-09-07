import json, pandas as pd, sys
sys.stdout.reconfigure(encoding='utf-8')

EXCEL_FILE = "EXPORT_20260216_100124.XLSX"
JSON_FILE  = "final_output_v8.json"

# --- 1. Load Excel material map ---
df = pd.read_excel(EXCEL_FILE, dtype=str, engine='openpyxl')
print("Excel columns:", list(df.columns))
print(f"Excel rows: {len(df)}")
print()

# Build material_map the same way as enrich_results.py
so_col = [c for c in df.columns if "One SO per PO" in c]
material_map = {}   # (cust_id, cust_mat) -> internal_mat
for _, row in df.iterrows():
    cust_id = str(row.get("Customer", "")).strip()
    if cust_id.endswith(".0"): cust_id = cust_id[:-2]
    if not cust_id or cust_id.lower() == "nan": continue

    cust_mat     = str(row.get("Customer Material Number", "")).strip()
    internal_mat = str(row.get("Material", "")).strip()
    if cust_mat.endswith(".0"): cust_mat = cust_mat[:-2]
    if internal_mat.endswith(".0"): internal_mat = internal_mat[:-2]

    if cust_mat and internal_mat and cust_mat.lower() != "nan" and internal_mat.lower() != "nan":
        material_map[(cust_id, cust_mat)] = internal_mat

print(f"Total material mappings in Excel: {len(material_map)}")
print("\nSample Excel material_map keys (first 20):")
for k, v in list(material_map.items())[:20]:
    print(f"  cust_id={k[0]!r:18}  cust_mat={k[1]!r:20}  ->  internal={v!r}")

# --- 2. Load JSON enriched output ---
with open(JSON_FILE, encoding='utf-8') as f:
    data = json.load(f)

print(f"\n\nJSON records: {len(data)}")
print("\n=== MATERIAL MATCHING DIAGNOSIS ===")
print(f"{'Source File':<50} {'Matched CustID':<16} {'Extracted MatCode':<25} {'Internal Mat':<15} {'Status'}")
print("-"*125)

missing_count = 0
mapped_count  = 0
for r in data:
    if 'error' in r:
        continue
    h        = r.get("header_fields", {})
    sold_to  = h.get("sold_to_id", "")
    cust_num = h.get("customer_number", "")
    fname    = r.get("source_file", "")[:48]

    # Try same ID priority as enrich_results.py
    matched_id = None
    for id_val in [sold_to, cust_num, h.get("ship_to_id", "")]:
        if not id_val: continue
        sid = str(id_val).strip()
        # Check direct in material_map keys
        candidates = [(k, v) for k, v in material_map.items() if k[0] == sid or k[0] == sid.lstrip("0")]
        if candidates:
            matched_id = sid
            break

    for item in r.get("line_items", []):
        cust_mat   = item.get("material_code", "") or ""
        int_mat    = item.get("internal_material")
        cust_mat_c = str(cust_mat).strip()
        if cust_mat_c.endswith(".0"): cust_mat_c = cust_mat_c[:-2]

        if int_mat:
            status = "OK"
            mapped_count += 1
        else:
            status = "MISSING"
            missing_count += 1
            # Show what we tried
            tried_id = matched_id or sold_to or cust_num
            # Did the key exist in map with different cust_mat?
            same_cust_keys = [(k[1], v) for k, v in material_map.items() if k[0] == tried_id or k[0] == (tried_id.lstrip("0") if tried_id else "")]
            if same_cust_keys:
                key_preview = " | ".join(f"{k}->{v}" for k, v in same_cust_keys[:3])
            else:
                key_preview = "(no Excel entry for this customer)"

        print(f"  {fname:<50} {(matched_id or sold_to or '?'):<16} {cust_mat_c:<25} {str(int_mat):<15} [{status}]")
        if int_mat is None:
            # Show what keys exist for this customer
            tried_id = (matched_id or sold_to or cust_num or "").strip()
            same_cust_keys = [(k[1], v) for k, v in material_map.items() if k[0] == tried_id or k[0] == tried_id.lstrip("0")]
            if same_cust_keys:
                print(f"    -> Excel has these cust_mat codes for {tried_id}:", [k for k,v in same_cust_keys[:5]])
            else:
                print(f"    -> No Excel material entries found for cust_id={tried_id!r}")

print(f"\n\nSUMMARY: Mapped={mapped_count}, Missing={missing_count}")
