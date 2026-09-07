import pandas as pd, sys, re
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

df = pd.read_parquet(".celonis_cache/test_mp_customer_master.parquet")
print(f"Test MP: {len(df)} rows")
print()

checks = [
    ("Bourbon AP Nitra SRO",  "SK2021830074", "PA60081"),
    ("Arcelik",               "144.902",       "9711870"),
    ("CEGAN",                 "26274744",       "x1022600"),
]

for cname, cid, mat in checks:
    print(f"=== Checking: {cname} ===")
    # Search by name
    mask = df["sold_to_name_full"].str.contains(cname[:6], case=False, na=False)
    hits = df[mask]
    print(f"  Name search ('{cname[:6]}'): {len(hits)} rows")
    if len(hits) > 0:
        print("  Sample sold_to_ids:", hits["sold_to_id"].unique()[:5].tolist())
    
    # Search by city/postcode from customer ID hint
    mask2 = df["sold_to_postcode"].str.contains(cid[:5], case=False, na=False) if len(cid) >= 5 else pd.Series([False]*len(df))
    hits2 = df[mask2]
    if len(hits2) > 0:
        print(f"  Postcode search ('{cid[:5]}'): {len(hits2)} rows")

    # Search material
    mask3 = df["customer_material_number"].str.contains(mat, case=False, na=False)
    hits3 = df[mask3]
    print(f"  Material '{mat}': {len(hits3)} rows in Test MP")
    if len(hits3) > 0:
        print("  Sample:", hits3[["sold_to_id","sold_to_name_full","customer_material_number"]].head(3).to_string())
    print()
