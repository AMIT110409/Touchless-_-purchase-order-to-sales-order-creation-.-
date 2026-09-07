"""
Diagnostic: Fetch actual table contents from Celonis Data Pool table 'PO_EXTRACTION_RESULTS'.
Checks the total rows, latest RUN_TIMESTAMPs, and verifies if 2026-07-31 rows exist in Celonis.
"""
import sys, os, io
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
os.environ['DISABLE_MODEL_SOURCE_CHECK'] = 'True'
from dotenv import load_dotenv
load_dotenv()

from pycelonis import get_celonis
import pandas as pd

url        = 'https://envalior-sb.eu-1.celonis.cloud/'
api_token  = 'NTFkY2QyZTQtMDQ4OS00MTljLThhMGUtMWJkMGRlOTUxNTkzOk5KM0RITkh2eVJvZmt6Z0ZvU05tT0s2MHRwMnc5cGhtaGw1a1l3L0tkTFAx'
pool_id    = '663e7e2d-74f0-4cf1-a0b4-ca4faf5fee06'
table_name = 'PO_EXTRACTION_RESULTS'

print("=" * 70)
print("  Celonis Data Pool Table Audit — 'PO_EXTRACTION_RESULTS'")
print("=" * 70)

c = get_celonis(url, api_token)
data_pool = c.data_integration.get_data_pool(pool_id)
table_obj = data_pool.get_table(table_name)

print(f"Table Name: {table_obj.name}")

# Export table data via Celonis export API
import requests
export_url = f"{url.rstrip('/')}/integration/api/v1/pools/{pool_id}/tables/{table_obj.id}/data/export"
headers_api = {
    'Authorization': f'Bearer {api_token}',
    'Content-Type': 'application/json',
}
resp = requests.post(export_url, headers=headers_api, json={'format': 'CSV'}, timeout=120)

if resp.status_code == 200:
    df = pd.read_csv(io.StringIO(resp.text), dtype=str)
    print(f"\n[OK] Fetched {len(df)} total rows from Celonis table 'PO_EXTRACTION_RESULTS'.")
    print(f"Columns: {list(df.columns)}")

    # Check RUN_TIMESTAMP breakdown
    if 'RUN_TIMESTAMP' in df.columns:
        print("\n--- RUN_TIMESTAMP value counts (Top 15 most recent timestamps) ---")
        df['RUN_TS_CLEAN'] = df['RUN_TIMESTAMP'].astype(str).str[:10]
        date_counts = df['RUN_TS_CLEAN'].value_counts()
        print(date_counts.to_string())

        print("\n--- Rows with RUN_TIMESTAMP from July 31 / August 1 ---")
        mask_recent = df['RUN_TS_CLEAN'].str.contains('2026-07-31|2026-08-01', na=False)
        recent_df = df[mask_recent]
        if not recent_df.empty:
            print(f"Found {len(recent_df)} rows pushed on July 31 / Aug 01:")
            cols_show = [c for c in ['PO_NUMBER', 'CUSTOMER_NAME', 'SOLD_TO_ID', 'SOURCE_FILE', 'RUN_TIMESTAMP'] if c in df.columns]
            print(recent_df[cols_show].to_string(max_colwidth=40))
        else:
            print("WARNING: No rows with RUN_TIMESTAMP 2026-07-31 found!")
else:
    print(f"ERROR downloading table: HTTP {resp.status_code}: {resp.text[:300]}")
