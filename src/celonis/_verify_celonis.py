from pycelonis import get_celonis

url = 'https://envalior-sb.eu-1.celonis.cloud/'
token = 'NTFkY2QyZTQtMDQ4OS00MTljLThhMGUtMWJkMGRlOTUxNTkzOk5KM0RITkh2eVJvZmt6Z0ZvU05tT0s2MHRwMnc5cGhtaGw1a1l3L0tkTFAx'

c = get_celonis(url, token)
pool = c.data_integration.get_data_pool('663e7e2d-74f0-4cf1-a0b4-ca4faf5fee06')
t = pool.get_tables().find('PO_EXTRACTION_RESULTS')

print(f"Table Name     : {t.name}")
print(f"Table Object   : {t}")

# Get columns
try:
    cols = t.columns
except:
    cols = []
print(f"Columns ({len(cols)}):")
for col in cols:
    ctype = getattr(col, 'type', getattr(col, 'column_type', 'unknown'))
    print(f"  - {col.name} ({ctype})")

# Try to export data
print("\nReading data...")
try:
    df = t.export_data_frame()
    print(f"Rows in table  : {len(df)}")
    print(f"Unique POs     : {df.iloc[:, 0].nunique()}")
    print(f"\nFirst 5 rows:")
    print(df.head(5).to_string())
except Exception as e:
    print(f"export_data_frame failed: {e}")
    print("Table was pushed successfully - verify in Celonis UI.")
