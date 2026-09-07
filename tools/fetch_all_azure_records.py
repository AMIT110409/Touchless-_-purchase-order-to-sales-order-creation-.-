"""
fetch_all_azure_records.py
==========================
Fetches ALL records from Azure Blob Storage (force refresh, bypass cache):
  1. so_creation_results.parquet  → SO_CREATION_RESULTS_azure_fresh.csv
  2. sheet3_order_mapping.parquet → SO creation tracking from Celonis order history
  3. Azure Table 'processedemails' → All processed email records

Exports clean CSVs for user review.
"""

import sys, io, os, pandas as pd
from pathlib import Path
from dotenv import load_dotenv
from azure.storage.blob import BlobServiceClient
from azure.identity import DefaultAzureCredential

load_dotenv()
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

ACCOUNT   = "poextstorage49245"
CONTAINER = "celonis-tables"
BLOB_URL  = f"https://{ACCOUNT}.blob.core.windows.net"
CACHE_DIR = Path(".celonis_cache")
CACHE_DIR.mkdir(exist_ok=True)

print("=" * 60)
print("Connecting to Azure Blob Storage...")
print("=" * 60)

cred = DefaultAzureCredential()
bsc  = BlobServiceClient(account_url=BLOB_URL, credential=cred)
container = bsc.get_container_client(CONTAINER)

# List all blobs
blobs = list(container.list_blobs())
print(f"Found {len(blobs)} blobs in '{CONTAINER}':")
for b in blobs:
    print(f"  {b.name}  ({b.size} bytes,  modified {b.last_modified})")

print()

# ─── Download each parquet blob (force refresh) ───────────────────────────────
def download_parquet(blob_name, local_name=None):
    local_name = local_name or blob_name
    local_path = CACHE_DIR / local_name
    print(f"Downloading: {blob_name} → {local_path}")
    data = container.download_blob(blob_name).readall()
    local_path.write_bytes(data)
    df = pd.read_parquet(local_path)
    print(f"  → {len(df)} rows, columns: {list(df.columns)[:8]}...")
    return df

# ─── 1. SO Creation Results ───────────────────────────────────────────────────
print("=" * 60)
print("1. SO CREATION RESULTS (so_creation_results.parquet)")
print("=" * 60)
try:
    df_so = download_parquet("so_creation_results.parquet")
    # Clean up doubled names
    for col in df_so.select_dtypes(include='object').columns:
        df_so[col] = df_so[col].fillna('').astype(str).str.strip()
    export_cols = [c for c in ['PO_NUMBER','SO_NUMBER','STATUS','BLOCK_CODE','BLOCK_REASON',
                                'FAILURE_REASON','SALES_ORG','CUSTOMER_NAME','SOURCE_FILE',
                                'RUN_TIMESTAMP'] if c in df_so.columns]
    df_so[export_cols].to_csv("SO_CREATION_RESULTS_azure_fresh.csv", index=False, encoding='utf-8-sig')
    print(f"  ✅ Exported {len(df_so)} rows → SO_CREATION_RESULTS_azure_fresh.csv")
    print(f"  Status counts: {df_so['STATUS'].value_counts().to_dict() if 'STATUS' in df_so.columns else 'N/A'}")
except Exception as e:
    print(f"  ❌ Error: {e}")

print()

# ─── 2. Check for any backup blobs we created ─────────────────────────────────
print("=" * 60)
print("2. BACKUP FILES IN Azure (celonis-tables/backups/)")
print("=" * 60)
backup_blobs = [b for b in blobs if 'backup' in b.name.lower() or 'PO_EXTRACTION' in b.name]
if backup_blobs:
    for b in backup_blobs:
        print(f"  {b.name}  ({b.size} bytes,  {b.last_modified})")
        # Download backup
        local_path = CACHE_DIR / Path(b.name).name
        data = container.download_blob(b.name).readall()
        local_path.write_bytes(data)
        try:
            df_bk = pd.read_csv(local_path, dtype=str)
            print(f"    → {len(df_bk)} rows")
        except:
            print(f"    → Downloaded (not CSV)")
else:
    print("  No backup blobs found yet.")

print()

# ─── 3. Azure Table: processedemails ─────────────────────────────────────────
print("=" * 60)
print("3. PROCESSED EMAILS (Azure Table: processedemails)")
print("=" * 60)
try:
    from azure.data.tables import TableServiceClient
    table_acct_url = f"https://{ACCOUNT}.table.core.windows.net"
    tsc = TableServiceClient(endpoint=table_acct_url, credential=cred)
    tc  = tsc.get_table_client("processedemails")
    entities = list(tc.list_entities())
    print(f"  Total processed email records: {len(entities)}")
    if entities:
        df_emails = pd.DataFrame(entities)
        df_emails.to_csv("PROCESSED_EMAILS_azure.csv", index=False, encoding='utf-8-sig')
        print(f"  ✅ Exported → PROCESSED_EMAILS_azure.csv")
        # Show key columns
        key_cols = [c for c in ['PartitionKey','RowKey','sender_email','subject',
                                  'status','processed_at','source_file'] if c in df_emails.columns]
        print(f"\n  Columns found: {list(df_emails.columns)}")
        print(f"\n  Sample (first 5):")
        print(df_emails[key_cols].head(5).to_string(index=False))
except Exception as e:
    print(f"  ❌ Error: {e}")

print()
print("=" * 60)
print("DONE — All Azure records fetched and exported.")
print("=" * 60)
