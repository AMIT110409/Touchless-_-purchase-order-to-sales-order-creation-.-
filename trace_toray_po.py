"""
Diagnose why TORAY PO 4503206365 failed to map / create order.
Checks: extracted data, CMIR lookup, material matching.
"""
import sys, os, json, glob
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
os.environ['DISABLE_MODEL_SOURCE_CHECK'] = 'True'
from dotenv import load_dotenv
load_dotenv()

TARGET_PO   = "4503206365"
TARGET_FILE = "4503206365"  # filename stem from attachment

print("=" * 70)
print(f"  TORAY PO {TARGET_PO} — Failure Trace")
print("=" * 70)

# ── 1. Find the enriched JSONL result for this PO ────────────────────────────
print("\n[1] Searching enriched result files for PO...")
search_dirs = [
    ".",
    "outlook_po_current_run",
    "outlook_po_extracted",
    "outlook_po_archive",
]
jsonl_files = []
for d in search_dirs:
    jsonl_files += glob.glob(f"{d}/**/*.jsonl", recursive=True)
    jsonl_files += glob.glob(f"{d}/*.jsonl", recursive=True)

found_rows = []
for fpath in set(jsonl_files):
    try:
        with open(fpath, encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                src = str(row.get("source_file", "") or "").lower()
                po  = str(row.get("po_number", "") or "").lower()
                cust_mat = str(row.get("customer_material_number", "") or "").lower()
                mat = str(row.get("material_number", "") or "").lower()
                cust = str(row.get("customer_name", "") or row.get("customer_id", "") or "").lower()
                if (TARGET_FILE.lower() in src or
                    TARGET_PO in po or
                    "toray" in cust or
                    "4503206365" in str(row)):
                    found_rows.append((fpath, row))
    except Exception:
        pass

if found_rows:
    for fpath, row in found_rows:
        print(f"\n  File: {fpath}")
        for k, v in row.items():
            if v not in (None, "", [], {}):
                print(f"    {k:<35}: {v}")
else:
    print("  No enriched rows found for this PO in JSONL files.")

# ── 2. Check Azure tracker record ────────────────────────────────────────────
print("\n[2] Azure Tracker record...")
from azure_email_tracker import AzureEmailTracker
tracker = AzureEmailTracker()
pending = tracker.get_eligible_emails(["PENDING", "MAPPING_FAILED", "EXTRACTION_FAILED",
                                        "MAPPED_SUCCESS", "PUSHED_TO_CELONIS"])
for r in pending:
    atts = r.get("attachments", "") or ""
    subj = r.get("subject", "") or ""
    if TARGET_PO in atts or TARGET_PO in subj or "TORAY" in subj.upper() or "toray" in atts.lower():
        print(f"\n  Status   : {r.get('status')}")
        print(f"  Subject  : {subj[:90]}")
        print(f"  Attach   : {atts[:120]}")
        print(f"  Source   : {r.get('source_file', '')}")
        print(f"  Updated  : {r.get('updated_at', '')}")

# ── 3. CMIR lookup for Q150E B-MB ────────────────────────────────────────────
print("\n[3] Checking CMIR for Q150E B-MB / Q150E B MB...")
try:
    import pandas as pd
    from pathlib import Path

    # Look for CMIR parquet / CSV in cache or local
    cmir_paths = (
        list(Path(".").glob("**/*cmir*.parquet")) +
        list(Path(".").glob("**/*cmir*.csv")) +
        list(Path(".celonis_cache").glob("*.parquet")) +
        list(Path(".celonis_cache").glob("*.csv"))
    )
    for cp in cmir_paths:
        print(f"  Checking: {cp}")
        try:
            if str(cp).endswith(".parquet"):
                df = pd.read_parquet(str(cp))
            else:
                df = pd.read_csv(str(cp))
            df.columns = [c.upper().strip() for c in df.columns]
            # Search for Q150 in any column
            mask = df.apply(lambda col: col.astype(str).str.contains("Q150", case=False, na=False)).any(axis=1)
            hits = df[mask]
            if not hits.empty:
                print(f"    Found {len(hits)} rows with Q150:")
                print(hits.to_string(max_cols=12, max_colwidth=30))
            else:
                print("    No Q150 rows found.")
        except Exception as e:
            print(f"    Error reading {cp}: {e}")
except ImportError:
    print("  pandas not available")

# ── 4. Re-run CMIR matching for 'Q150E B-MB' ─────────────────────────────────
print("\n[4] Re-running material lookup for customer_material='Q150E B-MB', customer=4020034383...")
try:
    from reenrich_results import _load_cmir_kb, _lookup_sap_material
    # Try to manually call the lookup
    print("  (checking reenrich_results for relevant functions)")
    import reenrich_results as rr
    funcs = [f for f in dir(rr) if not f.startswith("__")]
    print(f"  Available: {', '.join(funcs[:30])}")
except Exception as e:
    print(f"  Error: {e}")
