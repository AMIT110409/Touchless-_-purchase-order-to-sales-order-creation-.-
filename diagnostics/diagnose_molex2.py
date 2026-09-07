import pandas as pd, sys
sys.stdout.reconfigure(encoding="utf-8")

# Load Test MP cache
cache = ".celonis_cache/test_mp_customer_master.parquet"
df = pd.read_parquet(cache)
print(f"Test MP: {len(df)} rows, columns: {list(df.columns)}")
print()

# Search for Molex in any column
print("=== Searching for Molex in Test MP ===")
for col in df.columns:
    mask = df[col].astype(str).str.contains("molex|AN01009227077|Molex", case=False, na=False)
    hits = df[mask]
    if len(hits) > 0:
        print(f"  Found in column '{col}': {len(hits)} rows")
        print(hits[["sold_to_id", "sold_to_name_full", "customer_material_number"] 
                   if "sold_to_id" in df.columns else list(df.columns[:5])].head(3).to_string())
        print()

# Also search material 0899920756
print("=== Searching for material 0899920756 in Test MP ===")
mat_cols = [c for c in df.columns if "material" in c.lower()]
print(f"  Material columns: {mat_cols}")
for col in mat_cols:
    mask = df[col].astype(str).str.contains("0899920756", na=False)
    hits = df[mask]
    if len(hits) > 0:
        print(f"  Found in '{col}': {len(hits)} rows")
        print(hits.head(3).to_string())
        print()

if not any(df[col].astype(str).str.contains("0899920756", na=False).any() for col in mat_cols):
    print("  Material '0899920756' NOT FOUND in Test MP at all.")
