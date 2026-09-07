import pandas as pd, sys
sys.stdout.reconfigure(encoding="utf-8")

df = pd.read_parquet(".celonis_cache/test_mp_customer_master.parquet")
print("Test MP columns:", list(df.columns))
print()

# Check what data exists for NIHON MOLEX (4020013022)
rows = df[df["sold_to_id"] == "4020013022"]
print(f"NIHON MOLEX (4020013022) rows: {len(rows)}")
print(rows[["sold_to_id","sales_organization","ship_to_id","ship_to_city","ship_to_postcode","customer_material_number","material_internal"]].head(5).to_string())
