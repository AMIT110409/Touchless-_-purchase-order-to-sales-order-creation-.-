import pandas as pd, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
df = pd.read_parquet(".celonis_cache/test_mp_customer_master.parquet")

# Try many variations for Biko
for term in ["biko","bico","beiko","baiko","biku","iko","bike","bick"]:
    mask = df["sold_to_name_full"].str.contains(term, case=False, na=False)
    hits = df[mask][["sold_to_id","sold_to_name_full"]].drop_duplicates("sold_to_id")
    if len(hits) > 0:
        print(f"'{term}': {hits['sold_to_name_full'].iloc[0]} ({hits['sold_to_id'].iloc[0]})")
