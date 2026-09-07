import pandas as pd, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

df = pd.read_parquet(".celonis_cache/test_mp_customer_master.parquet")

# CEGAN materials - all of them
# NOTE: Two distinct SAP customers:
#   4020004125 = Cegan Production s.r.o  (old alias target - WRONG for X1022600 POs)
#   4020041175 = CEGAN s.r.o.            (correct for POs with article X1022600)
print("=== WRONG entity: Cegan Production s.r.o (4020004125) ===")
cegan_old = df[df["sold_to_id"] == "4020004125"]
print(f"Total rows: {len(cegan_old)}")
cols = ["customer_material_number","material_internal","material_description"]
print(cegan_old[cols].drop_duplicates().to_string())

print()
print("=== CORRECT entity: CEGAN s.r.o. (4020041175) ===")
cegan = df[df["sold_to_id"] == "4020041175"]
print(f"Total rows: {len(cegan)}")
print(cegan[cols].drop_duplicates().to_string())

print()
print("=== Searching for x1022600 or 1022600 across ALL customers ===")
for pattern in ["x1022600", "1022600", "X1022600", "Pocan B3235", "PBT Pocan"]:
    mask = (df["customer_material_number"].str.contains(pattern, case=False, na=False) |
            df["material_description"].str.contains(pattern, case=False, na=False))
    hits = df[mask]
    if len(hits) > 0:
        print(f"Pattern '{pattern}': {len(hits)} rows")
        print(hits[["sold_to_id","sold_to_name_full","customer_material_number","material_description","material_internal"]].drop_duplicates("customer_material_number").head(5).to_string())
    else:
        print(f"Pattern '{pattern}': 0 rows")
    print()
