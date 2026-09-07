"""
azure_table_reader.py
----------------------
Downloads Celonis reference tables from Azure Blob Storage (Parquet format)
and returns them as Pandas DataFrames.

Features:
  - Local cache: if .celonis_cache/<blob> exists and is < CACHE_TTL_HOURS old,
    skips download and reads from disk.
  - Works locally (DefaultAzureCredential → az login) and on Azure (Managed Identity).
  - Raises clear errors if files are missing so the caller can fall back gracefully.

Usage:
    from azure_table_reader import AzureTableReader

    reader = AzureTableReader()
    df_sheet2    = reader.get_sheet2()       # Customer Master Data
    df_sheet3    = reader.get_sheet3()       # Historical Order Mapping
    df_so_results = reader.get_so_results()  # SO Creation Results (Stage 2 feedback)
"""

import os
import io
import re
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

# ─── Configuration ────────────────────────────────────────────────────────────
AZURE_BLOB_URL    = os.getenv('AZURE_BLOB_URL', 'https://poextstorage49245.blob.core.windows.net')
CONTAINER_NAME    = os.getenv('AZURE_BLOB_CONTAINER_CELONIS', 'celonis-tables')
TEST_MP_BLOB      = os.getenv('CELONIS_TEST_MP_BLOB',    'test_mp_customer_master.parquet')
SHEET2_BLOB       = TEST_MP_BLOB   # backward-compat alias — now points to Test MP
SHEET3_BLOB       = os.getenv('CELONIS_SHEET3_BLOB',    'sheet3_order_mapping.parquet')
SO_RESULTS_BLOB   = os.getenv('CELONIS_SO_RESULTS_BLOB','so_creation_results.parquet')
CACHE_TTL_HOURS   = int(os.getenv('CELONIS_CACHE_TTL_HOURS', '24'))
CACHE_DIR         = Path(__file__).parent / '.celonis_cache'


# ─── Test MP column normalization ────────────────────────────────────────────

def _normalize_test_mp(df: pd.DataFrame) -> pd.DataFrame:
    """
    Normalize Test MP DataFrame after loading from parquet.

    Handles the case where Celonis exports columns with PQL expression prefixes
    like '#{o_custom_CustomerRoleMaterial.Material}' instead of 'Material'.
    Strips those prefixes, re-combines multi-line name/city/postcode fields,
    and applies the standard column rename.
    """

    # ── Step 1: Strip PQL expression prefixes ─────────────────────────────────
    # '#{o_custom_CustomerRoleMaterial.SoldToName1}' → 'SoldToName1'
    def strip_pql(col: str) -> str:
        m = re.match(r'#\{[^}]+\.([^}]+)\}', col)
        return m.group(1) if m else col

    df = df.rename(columns={c: strip_pql(c) for c in df.columns})

    # ── Step 2: Re-combine multi-line fields (may have been empty in cache) ───
    def combine_cols(df: pd.DataFrame, cols: list) -> pd.Series:
        result = pd.Series([''] * len(df), index=df.index, dtype=str)
        for col in cols:
            if col not in df.columns:
                continue
            part = df[col].fillna('').astype(str).str.strip()
            # Skip if this part is identical to the current result (avoids
            # "Pegasus Polymers Pte. Ltd. Pegasus Polymers Pte. Ltd." duplication
            # that occurs when Celonis stores the same name in Name1 and Name2).
            both_non_empty   = (result != '') & (part != '')
            already_same     = result.str.lower() == part.str.lower()
            should_append    = both_non_empty & ~already_same
            only_part        = (result == '') & (part != '')
            result[should_append] = result[should_append] + ' ' + part[should_append]
            result[only_part]     = part[only_part]
        return result.str.strip()

    # Only (re)combine if the combined column is missing or entirely empty
    def needs_combine(df, col):
        return col not in df.columns or df[col].fillna('').astype(str).str.strip().eq('').all()

    if needs_combine(df, 'sold_to_name_full'):
        df['sold_to_name_full'] = combine_cols(
            df, ['SoldToName1', 'SoldToName2', 'SoldToName3', 'SoldToName4'])
    if needs_combine(df, 'sold_to_city'):
        df['sold_to_city'] = combine_cols(df, ['SoldToCity1', 'SoldToCity2'])
    if needs_combine(df, 'sold_to_postcode'):
        df['sold_to_postcode'] = combine_cols(
            df, ['SoldToPostCode1', 'SoldToPostCode2', 'SoldToPostCode3'])
    if needs_combine(df, 'ship_to_name_full'):
        df['ship_to_name_full'] = combine_cols(
            df, ['ShipToName1', 'ShipToName2', 'ShipToName3', 'ShipToName4'])
    if needs_combine(df, 'ship_to_city'):
        df['ship_to_city'] = combine_cols(df, ['ShipToCity1', 'ShipToCity2'])
    if needs_combine(df, 'ship_to_postcode'):
        df['ship_to_postcode'] = combine_cols(
            df, ['ShipToPostCode1', 'ShipToPostCode2', 'ShipToPostCode3'])

    # ── Step 3: Rename remaining individual columns ───────────────────────────
    rename_map = {
        'SoldTo':                        'sold_to_id',
        'ShipTo':                        'ship_to_id',
        'SoldToCityCode':                'sold_to_city_code',
        'SoldToCitypCode':               'sold_to_cityp_code',
        'ShipToCityCode':                'ship_to_city_code',
        'ShipToCitypCode':               'ship_to_cityp_code',
        'Material':                      'material_internal',
        'MaterialDescription':           'material_description',
        'CustomerMaterialNumber':        'customer_material_number',
        'CustomerMaterialDescription':   'customer_material_description',  # PQL strips '1' suffix
        'CustomerMaterialDescription1':  'customer_material_description',
        'SalesOrg':                      'sales_organization',
        'CustomerGroup2':                'customer_group2',
        'EmployeeResponsible':           'salesperson_name',
        'EmployeeEmail':                 'salesperson_email',  # PQL strips space → 'EmployeeEmail'
        'Employee Email':                'salesperson_email',
    }
    rename_map = {k: v for k, v in rename_map.items() if k in df.columns}
    df = df.rename(columns=rename_map)

    return df


class AzureTableReader:
    """
    Reads Celonis reference tables from Azure Blob Storage with local disk cache.
    """

    def __init__(self, force_refresh: bool = False):
        """
        Args:
            force_refresh: If True, always download from Azure even if local cache is fresh.
        """
        self.force_refresh = force_refresh
        self._blob_service = None  # lazy init

    # ─── Public API ──────────────────────────────────────────────────────────

    def get_test_mp(self) -> pd.DataFrame:
        """
        Returns Test MP: Customer Master Data (replaces old New Sheet 2).

        Columns after normalization (combined at extraction time):
          sold_to_id              ← SoldTo (SAP customer number — primary key)
          sold_to_name_full       ← SoldToName1-4 combined
          sold_to_city            ← SoldToCity1-2 combined
          sold_to_postcode        ← SoldToPostCode1-3 combined
          sold_to_city_code       ← SoldToCityCode
          ship_to_id              ← ShipTo
          ship_to_name_full       ← ShipToName1-4 combined
          ship_to_city            ← ShipToCity1-2 combined
          ship_to_postcode        ← ShipToPostCode1-3 combined
          material_internal       ← Material
          material_description    ← MaterialDescription
          customer_material_number ← CustomerMaterialNumber
          customer_material_description ← CustomerMaterialDescription1
          sales_organization      ← SalesOrg
          customer_group2         ← CustomerGroup2  (Z01 = separate SO per item)
          salesperson_name        ← EmployeeResponsible
          salesperson_email       ← Employee Email
        """
        df = self._load(TEST_MP_BLOB, label='Test MP (Customer Master)')
        df = _normalize_test_mp(df)
        return df


    def get_sheet2(self) -> pd.DataFrame:
        """
        Returns Customer Master Data.
        Now an alias for get_test_mp() — loads from the Test MP blob.
        Kept for backward compatibility with existing callers.
        """
        return self.get_test_mp()

    def get_sheet3(self) -> pd.DataFrame:
        """
        Returns New Sheet 3: Historical Order Mapping.

        Columns (after normalization):
          customer_id, material_internal, sales_organization,
          sales_order, order_type, creation_date, customer_po_number
        """
        return self._load(SHEET3_BLOB, label='Sheet3 (Order Mapping)')

    def get_customer_master(self) -> pd.DataFrame:
        """Alias for get_sheet2()."""
        return self.get_sheet2()

    def get_so_results(self) -> pd.DataFrame:
        """
        Returns SO_CREATION_RESULTS: SAP Sales Order creation outcomes from the Action Flow.

        This is the Stage 2 feedback table. Populated by the Celonis Action Flow
        after it attempts SAP Sales Order creation. Used by run_celonis_feedback.py
        to decide whether to send Robona notifications or CSR exception emails.

        Columns (after normalization):
          PO_NUMBER, SO_NUMBER, STATUS ('SUCCESS'/'BLOCKED'/'FAILED'),
          BLOCK_CODE, BLOCK_REASON, FAILURE_REASON, SALES_ORG, RUN_TIMESTAMP
        """
        return self._load(SO_RESULTS_BLOB, label='SO_CREATION_RESULTS (Stage 2)')

    def get_order_history(self) -> pd.DataFrame:
        """Alias for get_sheet3()."""
        return self.get_sheet3()

    # ─── Internal Methods ─────────────────────────────────────────────────────

    def _load(self, blob_name: str, label: str) -> pd.DataFrame:
        """Load a Parquet blob — from local cache if fresh, else from Azure."""
        local_path = CACHE_DIR / blob_name

        # Check cache freshness
        if not self.force_refresh and self._cache_is_fresh(local_path):
            age_h = self._cache_age_hours(local_path)
            print(f"  [{label}] Using local cache (age: {age_h:.1f}h < {CACHE_TTL_HOURS}h): {local_path}")
            return pd.read_parquet(str(local_path), engine='pyarrow')

        # Download from Azure
        print(f"  [{label}] Downloading from Azure Blob Storage...")
        try:
            df = self._download_parquet(blob_name)

            # Save to local cache
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            df.to_parquet(str(local_path), index=False, engine='pyarrow')
            size_kb = local_path.stat().st_size / 1024
            print(f"  [{label}] Cached locally: {local_path}  ({size_kb:.1f} KB)")

            # Also save a human-readable CSV copy locally for easy inspection
            if blob_name == SO_RESULTS_BLOB or "so_creation_results" in blob_name:
                csv_path = Path("so_creation_results_local.csv")
                df.to_csv(csv_path, index=False, encoding="utf-8-sig")
                print(f"  [{label}] Exported local CSV copy -> {csv_path.name}")

            return df
        except Exception as e:
            if local_path.exists():
                age_h = self._cache_age_hours(local_path)
                print(f"  [WARN] Azure download failed: {e}. Falling back to expired local cache (age: {age_h:.1f}h).")
                return pd.read_parquet(str(local_path), engine='pyarrow')
            else:
                raise e

    def _cache_is_fresh(self, path: Path) -> bool:
        """True if path exists and was modified within CACHE_TTL_HOURS."""
        if not path.exists():
            return False
        age = datetime.now(timezone.utc) - datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        return age < timedelta(hours=CACHE_TTL_HOURS)

    def _cache_age_hours(self, path: Path) -> float:
        age = datetime.now(timezone.utc) - datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        return age.total_seconds() / 3600

    def _get_blob_service(self):
        """Lazy-init Azure Blob Service client."""
        if self._blob_service is None:
            from azure.storage.blob import BlobServiceClient
            account_key = os.getenv("AZURE_STORAGE_ACCOUNT_KEY")
            if account_key:
                self._blob_service = BlobServiceClient(
                    account_url=AZURE_BLOB_URL,
                    credential=account_key,
                )
            else:
                from azure.identity import DefaultAzureCredential
                credential = DefaultAzureCredential()
                self._blob_service = BlobServiceClient(
                    account_url=AZURE_BLOB_URL,
                    credential=credential,
                )
        return self._blob_service

    def _download_parquet(self, blob_name: str) -> pd.DataFrame:
        """Download a Parquet file from Azure Blob and return as DataFrame."""
        blob_service = self._get_blob_service()
        blob_client  = blob_service.get_blob_client(container=CONTAINER_NAME, blob=blob_name)

        try:
            download_stream = blob_client.download_blob()
            raw_bytes = download_stream.readall()
        except Exception as e:
            raise RuntimeError(
                f"Failed to download blob '{blob_name}' from container '{CONTAINER_NAME}': {e}\n"
                f"Make sure you have run 'python celonis_to_azure.py' first to populate the blob."
            ) from e

        df = pd.read_parquet(io.BytesIO(raw_bytes), engine='pyarrow')
        print(f"  -> Downloaded: {len(df)} rows, {len(df.columns)} columns.")
        return df


# ─── Convenience functions ────────────────────────────────────────────────────

def load_customer_master(force_refresh: bool = False) -> pd.DataFrame:
    """Quick one-liner: load Sheet 2 (Customer Master Data)."""
    return AzureTableReader(force_refresh=force_refresh).get_sheet2()


def load_order_history(force_refresh: bool = False) -> pd.DataFrame:
    """Quick one-liner: load Sheet 3 (Historical Order Mapping)."""
    return AzureTableReader(force_refresh=force_refresh).get_sheet3()


# ─── CLI self-test ────────────────────────────────────────────────────────────
if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Read Celonis tables from Azure Blob')
    parser.add_argument('--force-refresh', action='store_true', help='Force download even if cache is fresh')
    parser.add_argument('--sheet', choices=['sheet2', 'sheet3', 'both'], default='both')
    args = parser.parse_args()

    reader = AzureTableReader(force_refresh=args.force_refresh)

    print("\n" + "="*60)
    print("  Azure Table Reader — Self Test")
    print("="*60)

    if args.sheet in ('sheet2', 'both'):
        print("\n[1] Loading Sheet 2 (Customer Master)...")
        df2 = reader.get_sheet2()
        print(f"  Rows    : {len(df2)}")
        print(f"  Columns : {list(df2.columns)}")
        print(f"  Sample  :\n{df2.head(3).to_string()}\n")

    if args.sheet in ('sheet3', 'both'):
        print("\n[2] Loading Sheet 3 (Order Mapping)...")
        df3 = reader.get_sheet3()
        print(f"  Rows    : {len(df3)}")
        print(f"  Columns : {list(df3.columns)}")
        print(f"  Sample  :\n{df3.head(3).to_string()}\n")

    print("="*60)
    print("  Self-test complete.\n")
