"""
Audit backup state: registry, enriched JSONL files, and CSVs.
Investigates why Celonis table records were deleted.
"""
import sys, io, json, os, glob, datetime
import pandas as pd

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

print("=" * 60)
print("ROOT CAUSE: WHY RECORDS WERE DELETED FROM CELONIS")
print("=" * 60)
print("""
From the push_to_celonis.py log:
  [Celonis-WARN] Append failed (... Column "SENDER_EMAIL" is of type int
                 but expression is of type varchar) — schema mismatch.
  [Celonis] Re-creating table 'PO_EXTRACTION_RESULTS' with updated schema...
  [Celonis] SUCCESS: Re-created table 'PO_EXTRACTION_RESULTS' with 30 rows.

ROOT CAUSE: The SENDER_EMAIL column had dtype conflict (int vs varchar).
The fallback logic used drop_if_exists=True which DELETED the old table and
recreated it with ONLY the 30 rows from the current run's CSV.
All historical records from previous pushes were ERASED.
""")

print("=" * 60)
print("LOCAL PO PUSH REGISTRY")
print("=" * 60)
reg_path = os.path.join('.celonis_cache', 'pushed_po_registry.json')
if os.path.exists(reg_path):
    with open(reg_path, 'r', encoding='utf-8') as f:
        reg = json.load(f)
    print(f"  Total pushed keys: {reg.get('total', 0)}")
    print(f"  Last updated     : {reg.get('last_updated', 'unknown')}")
    print(f"  Keys stored      : {len(reg.get('pushed_keys', []))}")
else:
    print("  Registry file NOT found!")

print("\n" + "=" * 60)
print("ENRICHED JSONL FILES (local backups of ALL extracted POs)")
print("=" * 60)
enriched_files = sorted(
    [f for f in glob.glob('results_*_enriched.jsonl') if os.path.getsize(f) > 0],
    key=os.path.getmtime, reverse=True
)
for f in enriched_files:
    count = sum(1 for line in open(f, 'r', encoding='utf-8') if line.strip())
    mtime = datetime.datetime.fromtimestamp(os.path.getmtime(f)).strftime('%Y-%m-%d %H:%M')
    print(f"  {f}: {count} records  [{mtime}]")

print("\n" + "=" * 60)
print("CSV EXPORTS (ordered by most recent)")
print("=" * 60)
for f in sorted(glob.glob('extracted_pos_*.csv'), key=os.path.getmtime, reverse=True):
    df = pd.read_csv(f, dtype=str)
    mtime = datetime.datetime.fromtimestamp(os.path.getmtime(f)).strftime('%Y-%m-%d %H:%M')
    print(f"  {f}: {len(df)} rows  [{mtime}]")

print("\n" + "=" * 60)
print("ALL PO NUMBERS IN CURRENT ENRICHED JSONL")
print("=" * 60)
latest_enriched = enriched_files[0] if enriched_files else None
if latest_enriched:
    po_list = []
    with open(latest_enriched, 'r', encoding='utf-8') as f:
        for line in f:
            try:
                r = json.loads(line)
                po = r.get('header_fields', {}).get('po_number', '')
                src = r.get('source_file', '')
                if po and po not in po_list:
                    po_list.append(po)
            except:
                pass
    print(f"  Source: {latest_enriched}")
    print(f"  Unique POs: {len(po_list)}")
    for po in sorted(po_list):
        print(f"    {po}")
