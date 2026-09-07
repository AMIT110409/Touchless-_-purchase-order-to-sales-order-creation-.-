import pandas as pd, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

df = pd.read_parquet(".celonis_cache/test_mp_customer_master.parquet")
print("All columns in Test MP:")
for c in df.columns:
    print(f"  {c}")
print()

# Look for employee-related columns
emp_cols = [c for c in df.columns if "employ" in c.lower() or "email" in c.lower() 
            or "responsible" in c.lower() or "owner" in c.lower() 
            or "contact" in c.lower() or "csr" in c.lower() or "rep" in c.lower()]
print(f"Employee/Contact-related columns: {emp_cols}")
print()

if emp_cols:
    sample = df[emp_cols].dropna(how="all").head(10)
    print("Sample values:")
    print(sample.to_string())
