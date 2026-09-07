import pandas as pd, sys, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

df = pd.read_parquet(".celonis_cache/test_mp_customer_master.parquet")

# Check for Biko
print("=== Searching for BIKO in Test MP ===")
mask = df["sold_to_name_full"].str.contains("biko", case=False, na=False)
hits = df[mask][["sold_to_id","sold_to_name_full","customer_material_number","material_internal"]].drop_duplicates("sold_to_id")
print(f"Found {len(hits)} unique customers")
print(hits.head(10).to_string())

print()
# Show what CEGAN has in master data
print("=== CEGAN (4020004125) has only this in Test MP ===")
cegan = df[df["sold_to_id"] == "4020004125"]
print(cegan[["customer_material_number","material_internal","material_description"]].to_string())
print()
print("NOTE: material x1022600 (PBT Pocan B3235 GF30) is NOT in Test MP for CEGAN.")
print("It is mapped for OTHER customers (Pocan B3235 -> 000000000000050458)")
print("=> This material mapping needs to be ADDED to SAP master data for CEGAN 4020004125")

print()
# Show all current results
print("=== Current 3 POs from results_test_new_pos.jsonl ===")
with open("results_test_new_pos.jsonl") as f:
    for i, line in enumerate(f):
        rec = json.loads(line)
        hf = rec.get("header_fields", {})
        sos = rec.get("sales_orders", [{}])
        so = sos[0] if sos else {}
        items = so.get("items", [])
        item = items[0] if items else {}
        status = "MAPPED" if item.get("internal_material_number") else "NOT MAPPED"
        print(f"  PO {hf.get('po_number')} | Customer: {so.get('customer_name','?')} | "
              f"CustID: {so.get('customer_number','?')} | Material: {item.get('extracted_material_number','?')} "
              f"-> {item.get('internal_material_number','?')} | {status}")
