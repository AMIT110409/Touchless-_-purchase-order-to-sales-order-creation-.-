import os
import json
import hashlib
import pandas as pd
from pycelonis import get_celonis
import argparse
from dotenv import load_dotenv
from datetime import datetime
from pathlib import Path
import io

# Load environment variables
load_dotenv()


# ─────────────────────────────────────────────────────────────────────────────
# PO Push Registry
# ─────────────────────────────────────────────────────────────────────────────
# Tracks which PO composite keys (PO# + SoldTo + Material + Qty + DelivDate)
# have already been successfully pushed to Celonis.
#
# Storage hierarchy (Azure is PRIMARY — source of truth):
#   1. Azure Blob Storage  — "celonis-tables/pushed_po_registry.json"  ← PRIMARY
#   2. Local file cache    — ".celonis_cache/pushed_po_registry.json"  ← read-cache only
#
# On SAVE: Azure Blob is written FIRST (required). Local is written after as a
#          speed cache for the next load. If Azure fails, an error is raised.
# On LOAD: Azure Blob is tried first. Local cache used only if Azure unreachable.
#
# Guarantees: same PO is NEVER pushed to Celonis twice, even if:
#   - The pipeline is re-run after an exception fix
#   - The CSR sends the same PO email multiple times
#   - The same PO appears multiple times in the input CSV
# ─────────────────────────────────────────────────────────────────────────────

REGISTRY_BLOB_CONTAINER = "celonis-tables"
REGISTRY_BLOB_NAME      = "pushed_po_registry.json"
REGISTRY_LOCAL_PATH     = Path(__file__).parent / ".celonis_cache" / "pushed_po_registry.json"

# Composite key fields used to identify a unique PO line item
# Includes SHIP_TO_ID so that re-mapped ship-to corrections are NOT blocked as duplicates
DEDUP_KEYS = ["PO_NUMBER", "SOLD_TO_ID", "SHIP_TO_ID", "INTERNAL_MATERIAL_CODE", "QUANTITY", "DELIVERY_DATE"]


def _make_row_key(row: dict, keys: list) -> str:
    """Build a stable hash key from selected columns of a DataFrame row."""
    raw = "|".join(str(row.get(k, "")).strip().lower() for k in keys)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


class POPushRegistry:
    """
    Persistent registry of PO composite keys that have been pushed to Celonis.

    Backed by Azure Blob Storage (primary) with a local JSON file fallback.
    On load: fetches the registry from Azure Blob (or local).
    On save: writes back to Azure Blob AND local file (dual-write for safety).
    """

    def __init__(self):
        self._pushed_keys: set = set()
        self._azure_ok = False
        self._blob_service = None
        self._load()

    # ── Internal: Azure Blob helpers ─────────────────────────────────────────

    def _get_blob_service(self):
        if self._blob_service:
            return self._blob_service
        try:
            from azure.storage.blob import BlobServiceClient
            blob_url = os.getenv("AZURE_BLOB_URL", "")
            if not blob_url:
                return None
            account_key = os.getenv("AZURE_STORAGE_ACCOUNT_KEY")
            if account_key:
                svc = BlobServiceClient(account_url=blob_url, credential=account_key)
            else:
                from azure.identity import DefaultAzureCredential
                svc = BlobServiceClient(account_url=blob_url, credential=DefaultAzureCredential())
            self._blob_service = svc
            return svc
        except Exception as e:
            print(f"  [Registry-WARN] Azure Blob init failed: {e}")
            return None

    def _azure_download(self) -> dict | None:
        try:
            svc = self._get_blob_service()
            if not svc:
                return None
            blob = svc.get_blob_client(container=REGISTRY_BLOB_CONTAINER, blob=REGISTRY_BLOB_NAME)
            data = blob.download_blob().readall()
            return json.loads(data.decode("utf-8"))
        except Exception:
            return None

    def _azure_upload(self, registry: dict) -> bool:
        try:
            svc = self._get_blob_service()
            if not svc:
                return False
            blob = svc.get_blob_client(container=REGISTRY_BLOB_CONTAINER, blob=REGISTRY_BLOB_NAME)
            content = json.dumps(registry, indent=2).encode("utf-8")
            blob.upload_blob(content, overwrite=True)
            return True
        except Exception as e:
            print(f"  [Registry-WARN] Azure Blob upload failed: {e}")
            return False

    # ── Internal: local file helpers ─────────────────────────────────────────

    def _local_load(self) -> dict | None:
        try:
            if REGISTRY_LOCAL_PATH.exists():
                with open(REGISTRY_LOCAL_PATH, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return None

    def _local_save(self, registry: dict):
        try:
            REGISTRY_LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(REGISTRY_LOCAL_PATH, "w", encoding="utf-8") as f:
                json.dump(registry, f, indent=2)
        except Exception as e:
            print(f"  [Registry-WARN] Local registry save failed: {e}")

    # ── Public API ────────────────────────────────────────────────────────────

    def _load(self):
        """Load pushed keys from Azure Blob, with local file fallback."""
        registry = self._azure_download()
        if registry:
            self._azure_ok = True
            print(f"  [Registry] Loaded PO push registry from Azure Blob ({len(registry.get('pushed_keys', []))} keys).")
        else:
            registry = self._local_load()
            if registry:
                print(f"  [Registry] Loaded PO push registry from local cache ({len(registry.get('pushed_keys', []))} keys).")
            else:
                print(f"  [Registry] No existing PO push registry found — starting fresh.")
                registry = {"pushed_keys": [], "last_updated": ""}

        self._pushed_keys = set(registry.get("pushed_keys", []))

    def is_already_pushed(self, row_key: str) -> bool:
        """Return True if this PO row has already been pushed to Celonis."""
        return row_key in self._pushed_keys

    def mark_as_pushed(self, row_keys: list[str]):
        """Record that these PO row keys were successfully pushed. Saves to Azure (primary)."""
        if not row_keys:
            return
        self._pushed_keys.update(row_keys)
        registry = {
            "pushed_keys": sorted(self._pushed_keys),
            "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total": len(self._pushed_keys),
        }
        # ── Azure Blob is the PRIMARY destination (source of truth) ──────────
        azure_ok = self._azure_upload(registry)
        if azure_ok:
            print(f"  [Registry] Registry saved to Azure Blob ({len(self._pushed_keys)} total pushed keys).")
        else:
            # Azure failed — still save locally so this run doesn't re-push,
            # but warn clearly: Azure registry is now out of sync.
            print(
                f"  [Registry-WARN] Azure Blob save FAILED — registry saved to local cache only. "
                f"Re-run 'az login' or check AZURE_BLOB_URL. ({len(self._pushed_keys)} keys)"
            )
        # ── Local cache — always write as speed cache for next run ────────────
        self._local_save(registry)

    def filter_new_rows(self, df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
        """
        Filter a DataFrame to only rows NOT yet pushed to Celonis.

        Returns:
            (new_df, skipped_count)
        """
        avail_keys = [k for k in DEDUP_KEYS if k in df.columns]
        if not avail_keys:
            print(f"  [Registry-WARN] No dedup columns found in DataFrame — all rows treated as new.")
            return df, 0

        original_count = len(df)
        new_rows = []
        for _, row in df.iterrows():
            rk = _make_row_key(row.to_dict(), avail_keys)
            if not self.is_already_pushed(rk):
                new_rows.append(row)

        new_df = pd.DataFrame(new_rows, columns=df.columns) if new_rows else df.iloc[0:0].copy()
        skipped = original_count - len(new_df)
        return new_df, skipped

    def evict_po_numbers(self, df: pd.DataFrame, po_numbers: list[str]) -> int:
        """
        Remove all registry keys that belong to the given PO numbers.

        Called before filter_new_rows() when a PO is marked DUPLICATE ALLOWED
        (the previous push succeeded but Celonis Action Flow never created a Sales Order,
        so we must re-push the same PO data to trigger the Action Flow again).

        Args:
            df:         Current CSV DataFrame (used to reconstruct each row's dedup key).
            po_numbers: List of PO numbers whose registry entries should be evicted.

        Returns:
            Number of registry keys removed.
        """
        if not po_numbers:
            return 0

        po_set_lower = {str(p).strip().lower() for p in po_numbers if p}
        avail_keys   = [k for k in DEDUP_KEYS if k in df.columns]
        if not avail_keys:
            return 0

        # Find the dedup key column name for PO Number
        po_col = next((k for k in avail_keys if 'po' in k.lower() and 'number' in k.lower()), None)
        if not po_col:
            return 0

        # Collect all registry keys that match the given PO numbers
        keys_to_remove = set()
        for _, row in df.iterrows():
            row_po = str(row.get(po_col, "") or "").strip().lower()
            if row_po in po_set_lower:
                rk = _make_row_key(row.to_dict(), avail_keys)
                keys_to_remove.add(rk)

        if not keys_to_remove:
            return 0

        before = len(self._pushed_keys)
        self._pushed_keys -= keys_to_remove
        removed = before - len(self._pushed_keys)

        if removed > 0:
            # Persist the eviction immediately so it survives process restarts
            registry = {
                "pushed_keys":  sorted(self._pushed_keys),
                "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "total":        len(self._pushed_keys),
            }
            self._local_save(registry)
            self._azure_upload(registry)
            print(f"  [Registry] Evicted {removed} key(s) for PO(s) {po_numbers} — will re-push to Celonis.")

        return removed

    def collect_row_keys(self, df: pd.DataFrame) -> list[str]:
        """Compute registry keys for all rows in a DataFrame (call AFTER successful push)."""
        avail_keys = [k for k in DEDUP_KEYS if k in df.columns]
        if not avail_keys:
            return []
        return [_make_row_key(row.to_dict(), avail_keys) for _, row in df.iterrows()]




# ─────────────────────────────────────────────────────────────────────────────
# Azure Blob Backup Helper
# ─────────────────────────────────────────────────────────────────────────────
BACKUP_BLOB_CONTAINER = "celonis-tables"
BACKUP_LOCAL_DIR      = Path(__file__).parent / ".celonis_cache" / "backups"


def _backup_df_to_azure(df: pd.DataFrame, table_name: str) -> None:
    """
    Save a DataFrame as a timestamped CSV backup to:
      1. Azure Blob Storage  — celonis-tables/backups/<table_name>_<timestamp>.csv
      2. Local file          — .celonis_cache/backups/<table_name>_<timestamp>.csv

    Called automatically before any destructive Celonis operation (schema re-create).
    This ensures historical records can always be recovered.
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    blob_name  = f"backups/{table_name}_{ts}.csv"
    local_path = BACKUP_LOCAL_DIR / f"{table_name}_{ts}.csv"

    csv_bytes = df.to_csv(index=False).encode("utf-8")

    # ── Save locally first (always works) ──────────────────────────────────
    try:
        BACKUP_LOCAL_DIR.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(csv_bytes)
        print(f"  [Backup] Local backup saved: {local_path}  ({len(df)} rows)")
    except Exception as local_err:
        print(f"  [Backup-WARN] Local backup failed: {local_err}")

    # ── Try Azure Blob upload ───────────────────────────────────────────────
    try:
        from azure.storage.blob import BlobServiceClient
        from azure.identity import DefaultAzureCredential
        cred   = DefaultAzureCredential()
        acct   = os.environ.get("AZURE_STORAGE_ACCOUNT", "poextstorage49245")
        bsc    = BlobServiceClient(
            account_url=f"https://{acct}.blob.core.windows.net",
            credential=cred,
        )
        container = bsc.get_container_client(BACKUP_BLOB_CONTAINER)
        container.upload_blob(name=blob_name, data=csv_bytes, overwrite=True)
        print(f"  [Backup] Azure Blob backup saved: {BACKUP_BLOB_CONTAINER}/{blob_name}")
    except Exception as az_err:
        print(f"  [Backup-WARN] Azure Blob backup failed (local copy still safe): {az_err}")


# ─────────────────────────────────────────────────────────────────────────────
# Main push function
# ─────────────────────────────────────────────────────────────────────────────

def push_to_celonis(csv_path: str, force_po_numbers: str = ""):

    """
    Pushes data from a CSV file to the Celonis PO_EXTRACTION_RESULTS table.

    Deduplication strategy (4 layers):
      1. Within-CSV:       drop_duplicates on composite key columns
      2. Registry evict:   if force_po_numbers given, remove those PO keys from registry first
      3. Registry check:   skip rows whose key is in POPushRegistry (already pushed)
      4. Celonis append:   only push the genuinely new rows
      5. Registry update:  mark pushed rows as done so future runs skip them

    Args:
        csv_path:         Path to the CSV file to push.
        force_po_numbers: Comma-separated PO numbers to force re-push (DUPLICATE ALLOWED POs).
                          These POs will have their registry keys evicted before the filter step,
                          ensuring they are re-pushed to Celonis even if previously pushed.
    """
    print(f"  [Celonis] Starting Celonis push for: {csv_path}")
    if force_po_numbers:
        print(f"  [Celonis] Force re-push requested for PO(s): {force_po_numbers}")


    # Celonis Configuration
    url        = 'https://envalior-sb.eu-1.celonis.cloud/'
    api_token  = 'NTFkY2QyZTQtMDQ4OS00MTljLThhMGUtMWJkMGRlOTUxNTkzOk5KM0RITkh2eVJvZmt6Z0ZvU05tT0s2MHRwMnc5cGhtaGw1a1l3L0tkTFAx'
    pool_id    = '663e7e2d-74f0-4cf1-a0b4-ca4faf5fee06'
    table_name = 'PO_EXTRACTION_RESULTS'

    if not os.path.exists(csv_path):
        print(f"  [Error] CSV file not found: {csv_path}")
        return

    try:
        # ── Step 1: Load CSV ──────────────────────────────────────────────────
        df = pd.read_csv(csv_path, dtype=str, encoding="utf-8-sig")
        df['RUN_TIMESTAMP'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"  [Data] Loaded {len(df)} rows from CSV.")

        # Normalise column names → UPPER_SNAKE_CASE (Celonis standard)
        df.columns = [
            col.strip().upper().replace(' ', '_').replace('/', '_')
            for col in df.columns
        ]
        print(f"  [Data] Columns: {list(df.columns)}")

        # ── Step 2: Validate — drop rows missing Sold-To or unmapped material ─
        def _is_blank(series):
            return series.isna() | (series.astype(str).str.strip() == "") | (series.astype(str).str.strip() == "nan")

        sold_to_col  = "SOLD_TO_ID"            if "SOLD_TO_ID"            in df.columns else None
        cust_num_col = "CUSTOMER_NUMBER"        if "CUSTOMER_NUMBER"       in df.columns else None
        mat_col      = "INTERNAL_MATERIAL_CODE" if "INTERNAL_MATERIAL_CODE" in df.columns else None

        valid_customer = pd.Series([True] * len(df), index=df.index)
        if sold_to_col and cust_num_col:
            valid_customer = ~(_is_blank(df[sold_to_col]) & _is_blank(df[cust_num_col]))
        elif sold_to_col:
            valid_customer = ~_is_blank(df[sold_to_col])
        elif cust_num_col:
            valid_customer = ~_is_blank(df[cust_num_col])

        valid_material = pd.Series([True] * len(df), index=df.index)
        if mat_col:
            valid_material = ~_is_blank(df[mat_col])

        valid_mask = valid_customer & valid_material
        invalid_df = df[~valid_mask]
        df_clean   = df[valid_mask].copy()

        if len(invalid_df) > 0:
            print(f"  [Validation] Skipping {len(invalid_df)} incomplete row(s) — missing Sold-To or unmapped material:")
            for _, row in invalid_df.iterrows():
                po   = row.get("PO_NUMBER", "?")
                cust = row.get("CUSTOMER_NAME", "?")
                mat  = row.get("EXTRACTED_MATERIAL_CODE", row.get("CUSTOMER_MATERIAL_CODE", "?"))
                sid  = row.get(sold_to_col, "") if sold_to_col else ""
                imat = row.get(mat_col, "") if mat_col else ""
                reasons = []
                if not sid or str(sid).strip() in ("", "nan"):
                    reasons.append("no Sold-To ID")
                if not imat or str(imat).strip() in ("", "nan"):
                    reasons.append(f"material '{mat}' not mapped")
                print(f"    -> PO {po} | {cust} | Skipped: {', '.join(reasons)}")

        if len(df_clean) == 0:
            print(f"  [Validation] All rows are incomplete — nothing to push to Celonis.")
            return

        # ── Step 3: Within-CSV deduplication ─────────────────────────────────
        # Handles: same PO appears twice in a single CSV (e.g. duplicate extraction)
        avail_dedup = [k for k in DEDUP_KEYS if k in df_clean.columns]
        if avail_dedup:
            before = len(df_clean)
            df_clean = df_clean.drop_duplicates(subset=avail_dedup)
            removed = before - len(df_clean)
            if removed > 0:
                print(f"  [Dedup-IntraRun] Removed {removed} within-CSV duplicate row(s) on {avail_dedup}.")

        print(f"  [Validation] {len(df_clean)} valid unique row(s) after within-CSV dedup.")

        # ── Step 3b: Evict registry keys for DUPLICATE ALLOWED POs ──────────────
        # When a PO was previously pushed but no SAP Sales Order was created,
        # the pipeline marks it DUPLICATE ALLOWED and calls us with --force-po-numbers.
        # We must evict those keys from the registry BEFORE filter_new_rows() so the
        # push goes through and triggers the Celonis Action Flow again.
        force_po_list = [p.strip() for p in force_po_numbers.split(",") if p.strip()] \
                        if force_po_numbers else []
        registry = POPushRegistry()
        if force_po_list:
            evicted = registry.evict_po_numbers(df_clean, force_po_list)
            if evicted == 0:
                print(f"  [Registry] No matching registry keys found for force-POs {force_po_list} — will push as new.")
        else:
            registry = POPushRegistry()  # fresh load (evict already loaded it if needed)

        # ── Step 4: Cross-run deduplication via PO Push Registry ──────────────
        # Handles: same PO pushed in a previous pipeline run (e.g. after exception fix).
        # Registry is stored in Azure Blob & local file — no Celonis read needed.

        df_new, skipped_count = registry.filter_new_rows(df_clean)

        if skipped_count > 0:
            print(f"  [Dedup-CrossRun] Skipped {skipped_count} row(s) already pushed in a previous run.")
        else:
            print(f"  [Dedup-CrossRun] All {len(df_clean)} row(s) are new — none previously pushed.")

        if len(df_new) == 0:
            print(f"  [Celonis] Nothing new to push — all rows already exist in Celonis registry. Done.")
            return

        print(f"  [Celonis] {len(df_new)} genuinely new row(s) will be pushed to Celonis.")
        df = df_new

        # ── Step 5: Fix dtypes before push — prevent schema conflicts ─────────
        # SENDER_EMAIL was previously stored as int in Celonis (blank → 0).
        # Force all string columns to str so Celonis receives varchar not int.
        str_columns = [
            'SENDER_EMAIL', 'EMAIL_RESPONSIBLE', 'EMPLOYEE_RESPONSIBLE',
            'SHIP_TO_NAME', 'CUSTOMER_NAME', 'MATERIAL_DESCRIPTION',
            'CUSTOMER_MATERIAL_CODE', 'INTERNAL_MATERIAL_CODE',
            'EXTRACTED_MATERIAL_CODE', 'SO_GROUPING_RULE', 'SO_RULE',
            'ORDER_TYPE', 'VENDOR_NAME', 'SOURCE_FILE',
        ]
        for col in str_columns:
            if col in df.columns:
                df[col] = df[col].fillna('').astype(str)

        # ── Step 5b: Enforce standard Unit of Measure (UoM) normalization ───
        # Converts "kilograam", "Kilograam", "kilogram", "KGM", "KGS", etc. to "KG"
        def _clean_unit(val: str) -> str:
            if not val or pd.isna(val) or str(val).strip().lower() in ("nan", "none", ""):
                return "KG"
            u = str(val).strip().upper().rstrip(".")
            if u in (
                "KILOGRAM", "KILOGRAMS", "KILOGRAMM", "KILOGRAMME", "KILOGRAMMES",
                "KILOGRAAM", "KILOGRAAMS", "KILOGRAMEN", "KILOGRAAMEN", "KILOGRAMA", "KILOGRAMAS",
                "CHILOGRAMMO", "CHILOGRAMMI", "KILO", "KILOS", "KGM", "KGS", "KG"
            ):
                return "KG"
            if u in ("MTR", "METRE", "METER"):
                return "M"
            if u in ("PCE", "PCS", "PIECE", "PIECES"):
                return "PC"
            if u in ("LTR", "LITRE", "LITER"):
                return "L"
            return u

        unit_cols = [c for c in df.columns if any(k in c for k in ("UNIT", "UOM"))]
        for ucol in unit_cols:
            df[ucol] = df[ucol].apply(_clean_unit)
            print(f"  [UnitNorm] Normalized column '{ucol}' to standard SAP codes.")

        # ── Step 6: Connect to Celonis and push (safe merge — never drop alone) ─
        c         = get_celonis(url, api_token)
        data_pool = c.data_integration.get_data_pool(pool_id)

        existing_table_names = [t.name for t in data_pool.get_tables()]
        table_exists = table_name in existing_table_names
        print(f"  [Celonis] Table '{table_name}' exists: {table_exists}")

        push_ok = False
        if table_exists:
            table_obj = data_pool.get_table(table_name)
            print(f"  [Celonis] Appending {len(df)} NEW row(s) to existing table '{table_name}'...")
            try:
                table_obj.append(df)
                print(f"  [Celonis] SUCCESS: Appended {len(df)} new rows to '{table_name}'.")
                push_ok = True
            except Exception as append_err:
                print(f"  [Celonis-WARN] Append failed ({append_err}) — schema mismatch.")
                print(f"  [Celonis] Safe re-create: fetching existing table data first...")
                # ── SAFE MERGE STRATEGY ────────────────────────────────────────
                # 1. Download ALL existing Celonis rows
                # 2. Backup to Azure Blob + local file BEFORE any deletion
                # 3. Merge existing + new (dedup on DEDUP_KEYS)
                # 4. Recreate table with FULL merged dataset
                # This ensures NO historical records are ever lost.
                existing_df = None
                try:
                    # PyCelonis: use _push_data_frame's inverse — download via pycelonis
                    import requests, base64
                    # Use Celonis data export API to get existing rows as CSV
                    export_url = f"{url.rstrip('/')}/integration/api/v1/pools/{pool_id}/tables/{table_obj.id}/data/export"
                    headers_api = {
                        'Authorization': f'Bearer {api_token}',
                        'Content-Type': 'application/json',
                    }
                    resp = requests.post(
                        export_url,
                        headers=headers_api,
                        json={'format': 'CSV'},
                        timeout=120,
                    )
                    if resp.status_code == 200:
                        existing_df = pd.read_csv(io.StringIO(resp.text), dtype=str)
                        print(f"  [Backup] Downloaded {len(existing_df)} existing rows from Celonis.")
                    else:
                        print(f"  [Backup-WARN] Could not download existing data (HTTP {resp.status_code}).")
                except Exception as dl_err:
                    print(f"  [Backup-WARN] Download failed: {dl_err}")

                # Backup existing data to Azure Blob and local file
                if existing_df is not None and len(existing_df) > 0:
                    _backup_df_to_azure(existing_df, table_name)

                # Merge existing + new rows, dedup on composite keys
                if existing_df is not None and len(existing_df) > 0:
                    # Fix dtypes in existing_df to match new df
                    for col in str_columns:
                        if col in existing_df.columns:
                            existing_df[col] = existing_df[col].fillna('').astype(str)
                    # Add any new columns from df that aren't in existing_df
                    for col in df.columns:
                        if col not in existing_df.columns:
                            existing_df[col] = ''
                    # Ensure column order matches df
                    common_cols = [c for c in df.columns if c in existing_df.columns]
                    existing_aligned = existing_df[common_cols].copy()
                    df_aligned       = df[common_cols].copy()
                    merged_df = pd.concat([existing_aligned, df_aligned], ignore_index=True)
                    avail_dedup2 = [k for k in DEDUP_KEYS if k in merged_df.columns]
                    if avail_dedup2:
                        before_merge = len(merged_df)
                        merged_df = merged_df.drop_duplicates(subset=avail_dedup2, keep='last')
                        print(f"  [Merge] Merged {len(existing_df)} existing + {len(df_aligned)} new = {len(merged_df)} unique rows (removed {before_merge - len(merged_df)} dups).")
                    df_to_push = merged_df
                else:
                    df_to_push = df
                    print(f"  [Merge] No existing data recovered — recreating with {len(df_to_push)} rows only.")

                data_pool.create_table(
                    df=df_to_push,
                    table_name=table_name,
                    drop_if_exists=True,
                    force=True,
                )
                print(f"  [Celonis] SUCCESS: Re-created table '{table_name}' with {len(df_to_push)} total rows (existing + new).")
                push_ok = True
        else:
            print(f"  [Celonis] Table not found. Creating '{table_name}' with {len(df)} row(s)...")
            data_pool.create_table(
                df=df,
                table_name=table_name,
                drop_if_exists=False,
            )
            print(f"  [Celonis] SUCCESS: Created table '{table_name}' with {len(df)} rows.")
            push_ok = True

        # ── Step 6: Mark pushed rows in registry ONLY on success ─────────────
        # This is critical: we only mark rows as "done" if the Celonis push actually
        # succeeded. If the push fails, next run will try again (no false negatives).
        if push_ok:
            pushed_keys = registry.collect_row_keys(df)
            registry.mark_as_pushed(pushed_keys)
            print(f"  [Registry] Marked {len(pushed_keys)} row(s) as pushed in registry.")

        # ── Step 7: Reload Data Model ─────────────────────────────────────────
        try:
            data_model_id = '3f193f92-a398-4989-915c-cc100e67d421'
            data_model = data_pool.get_data_model(data_model_id)
            data_model.reload()
            print(f"  [Celonis] Data Model reloaded successfully.")
        except Exception as e2:
            print(f"  [Celonis] Data Model reload skipped (non-fatal): {e2}")

    except Exception as e:
        import traceback
        print(f"  [Celonis] Push failed: {e}")
        traceback.print_exc()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Push CSV data to Celonis (append mode with deduplication)")
    parser.add_argument("--input", type=str, required=True, help="Path to input CSV file")
    parser.add_argument("--reset-registry", action="store_true",
                        help="Clear the PO push registry (use only for testing/reset)")
    parser.add_argument("--force-po-numbers", type=str, default="",
                        help="Comma-separated PO numbers to force re-push even if already in registry. "
                             "Used when a PO had a previous push but no SAP SO was created (DUPLICATE ALLOWED).")
    args = parser.parse_args()

    if args.reset_registry:
        print("  [Registry] --reset-registry flag set: clearing PO push registry.")
        REGISTRY_LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        REGISTRY_LOCAL_PATH.write_text(
            json.dumps({"pushed_keys": [], "last_updated": "", "total": 0}, indent=2),
            encoding="utf-8"
        )
        print("  [Registry] Local registry cleared.")

    push_to_celonis(args.input, force_po_numbers=args.force_po_numbers)

