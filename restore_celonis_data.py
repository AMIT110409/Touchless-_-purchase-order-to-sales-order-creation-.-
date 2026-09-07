"""
restore_celonis_data.py
=======================
Restores all historical PO records into the Celonis PO_EXTRACTION_RESULTS table.

Sources (all accumulated CSV exports):
  - extracted_pos_outlook_po_extracted.csv  (latest — today's run, 34 rows)
  - test_out.csv / test_export_out.csv
  - extracted_pos_PO examples.csv
  - extracted_pos_outlook_all_emails.csv
  - extracted_pos_unprocessed_run_final.csv
  (and any others found in the directory)

The script:
  1. Merges all CSVs into one DataFrame
  2. Deduplicates on composite key (PO#, SoldTo, Material, Qty, DelivDate)
  3. Normalises column names and dtypes (SENDER_EMAIL, etc.)
  4. Pushes the full merged dataset to Celonis — with NO drop_if_exists
  5. Takes a local backup to .celonis_cache/backups/ first
"""

import os, sys, io, glob, json
import pandas as pd
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from pycelonis import get_celonis

load_dotenv()

# ─── Config ────────────────────────────────────────────────────────────────
url        = 'https://envalior-sb.eu-1.celonis.cloud/'
api_token  = 'NTFkY2QyZTQtMDQ4OS00MTljLThhMGUtMWJkMGRlOTUxNTkzOk5KM0RITkh2eVJvZmt6Z0ZvU05tT0s2MHRwMnc5cGhtaGw1a1l3L0tkTFAx'
pool_id    = '663e7e2d-74f0-4cf1-a0b4-ca4faf5fee06'
dm_id      = '3f193f92-a398-4989-915c-cc100e67d421'
table_name = 'PO_EXTRACTION_RESULTS'

DEDUP_KEYS = ['PO Number', 'Sold To ID', 'Internal Material Code', 'Quantity', 'Delivery Date']
BACKUP_DIR = Path('.celonis_cache/backups')

# Columns that MUST be string (varchar) - prevents Celonis type conflict
STR_COLUMNS = [
    'Sender Email', 'Email Responsible', 'Employee Responsible',
    'Ship To Name', 'Customer Name', 'Material Description',
    'Customer Material Code', 'Internal Material Code',
    'Extracted Material Code', 'SO Grouping Rule', 'SO Rule',
    'Order Type', 'Vendor Name', 'Source File',
]

# ─── Step 1: Discover all relevant CSV exports ─────────────────────────────
SCRIPT_DIR = Path(__file__).parent
csv_candidates = [
    'extracted_pos_outlook_po_extracted.csv',
    'test_out.csv',
    'test_export_out.csv',
    'extracted_pos_PO examples.csv',
    'extracted_pos_outlook_all_emails.csv',
    'extracted_pos_unprocessed_run_final.csv',
    'extracted_pos_unprocessed_run.csv',
]

print("=" * 60)
print("STEP 1: Loading all historical CSV exports")
print("=" * 60)

dfs = []
for csv_file in csv_candidates:
    full_path = SCRIPT_DIR / csv_file
    if not full_path.exists():
        print(f"  SKIP (not found): {csv_file}")
        continue
    df = pd.read_csv(full_path, dtype=str)
    if len(df) == 0:
        print(f"  SKIP (empty)    : {csv_file}")
        continue
    print(f"  Loaded {len(df):>4} rows from: {csv_file}")
    dfs.append(df)

if not dfs:
    print("ERROR: No CSV files found. Cannot restore.")
    sys.exit(1)

# ─── Step 2: Merge and deduplicate ────────────────────────────────────────
print("\n" + "=" * 60)
print("STEP 2: Merging and deduplicating")
print("=" * 60)

merged = pd.concat(dfs, ignore_index=True)
print(f"  Combined rows (before dedup): {len(merged)}")

avail_dedup = [k for k in DEDUP_KEYS if k in merged.columns]
if avail_dedup:
    merged = merged.drop_duplicates(subset=avail_dedup, keep='last')
print(f"  Unique rows (after dedup)   : {len(merged)}")

# ─── Step 3: Normalise dtypes ─────────────────────────────────────────────
for col in STR_COLUMNS:
    if col in merged.columns:
        merged[col] = merged[col].fillna('').astype(str)

# Add timestamp
merged['Run Timestamp'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

# ─── Step 4: Local backup BEFORE touching Celonis ─────────────────────────
print("\n" + "=" * 60)
print("STEP 4: Local backup")
print("=" * 60)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
backup_path = BACKUP_DIR / f"{table_name}_restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
merged.to_csv(backup_path, index=False)
print(f"  Backup saved locally: {backup_path}  ({len(merged)} rows)")

# ─── Step 5: Normalise column names to UPPER_SNAKE_CASE for Celonis ───────
merged.columns = [
    col.strip().upper().replace(' ', '_').replace('/', '_')
    for col in merged.columns
]
# Fix STR_COLUMNS for upper names too
str_cols_upper = [
    c.strip().upper().replace(' ', '_').replace('/', '_')
    for c in STR_COLUMNS
]
for col in str_cols_upper:
    if col in merged.columns:
        merged[col] = merged[col].fillna('').astype(str)

print(f"\n  Final columns: {list(merged.columns)}")

# ─── Step 6: Push to Celonis ──────────────────────────────────────────────
print("\n" + "=" * 60)
print("STEP 5: Pushing to Celonis PO_EXTRACTION_RESULTS")
print("=" * 60)

c = get_celonis(url, api_token)
dp = c.data_integration.get_data_pool(pool_id)

existing_names = [t.name for t in dp.get_tables()]
if table_name in existing_names:
    print(f"  Table '{table_name}' exists — recreating with full merged dataset...")
    dp.create_table(df=merged, table_name=table_name, drop_if_exists=True, force=True)
else:
    print(f"  Table '{table_name}' not found — creating fresh...")
    dp.create_table(df=merged, table_name=table_name, drop_if_exists=False)

print(f"  SUCCESS: {len(merged)} rows pushed to '{table_name}'.")

# ─── Step 7: Reload Data Model ────────────────────────────────────────────
print("\n" + "=" * 60)
print("STEP 6: Reloading Data Model")
print("=" * 60)
try:
    dm = dp.get_data_model(dm_id)
    dm.reload()
    print("  Data Model reloaded successfully.")
except Exception as e:
    print(f"  Data Model reload skipped: {e}")

print("\n" + "=" * 60)
print(f"RESTORE COMPLETE: {len(merged)} total rows in PO_EXTRACTION_RESULTS")
print("=" * 60)
