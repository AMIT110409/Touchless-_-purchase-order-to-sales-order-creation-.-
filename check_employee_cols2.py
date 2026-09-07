import pandas as pd, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

df = pd.read_parquet(".celonis_cache/test_mp_customer_master.parquet")

# Show salesperson_name + salesperson_email together
print("Unique salesperson_name + salesperson_email pairs:")
cols = ["sold_to_id","sold_to_name_full","salesperson_name","salesperson_email"]
sample = df[cols].drop_duplicates(subset=["sold_to_id","salesperson_email"]).dropna(subset=["salesperson_email"])
print(f"Total unique customer-CSR pairings: {len(sample)}")
print()
print(sample.head(15).to_string())

print()
print("Unique CSR emails:", df["salesperson_email"].nunique())
print(df["salesperson_email"].value_counts().head(10))
