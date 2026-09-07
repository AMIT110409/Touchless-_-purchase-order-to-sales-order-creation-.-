"""
celonis_to_azure.py
-------------------
Extracts Celonis tables (New Sheet 2 and New Sheet 3) from the
Touchless Order Creation analysis and uploads them to Azure Blob Storage
as Parquet files.

Tables loaded:
  - New Sheet 2  → Customer Master Data (CustomerGroup2, SalespersonEmail, ...)
  - New Sheet 3  → Historical Order Mapping (Customer, Material, SalesOrg, OrderType, SalesOrder, ...)

Usage:
    python celonis_to_azure.py
    python celonis_to_azure.py --sheet2-only
    python celonis_to_azure.py --sheet3-only
    python celonis_to_azure.py --dry-run   # extract but don't upload
"""

import os
import sys
os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
sys.stdout.reconfigure(encoding='utf-8') if hasattr(sys.stdout, 'reconfigure') else None
import time
import argparse
import pandas as pd
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ─── Celonis Configuration (all values from .env — no hardcoded credentials) ──
CELONIS_URL     = os.getenv('CELONIS_URL')
API_TOKEN       = os.getenv('CELONIS_API_TOKEN')
POOL_ID         = os.getenv('CELONIS_POOL_ID')
DATA_MODEL_ID   = os.getenv('CELONIS_DATA_MODEL_ID')
ANALYSIS_ID     = os.getenv('CELONIS_ANALYSIS_ID', '')  # optional, for analysis-mode queries
SPACE_NAME      = os.getenv('CELONIS_SPACE_NAME', 'OCPM')
PACKAGE_NAME    = os.getenv('CELONIS_PACKAGE_NAME', 'Touchless Order Creation')

# Component IDs for each sheet
TEST_MP_NAME    = 'Test MP'           # Customer Master — replaces old New Sheet 2
TEST_MP_COMP_ID = os.getenv('CELONIS_TEST_MP_COMP_ID', None)  # Auto-discovered at runtime if None
SHEET3_NAME     = 'New Sheet 3'
SHEET3_COMP_ID  = os.getenv('CELONIS_SHEET3_COMP_ID', '48615e45-2c6c-4a12-ab8c-22107237c167')  # Historical Order Mapping component

# ─── Azure Blob Configuration ─────────────────────────────────────────────────
AZURE_BLOB_URL      = os.getenv('AZURE_BLOB_URL', 'https://poextstorage49245.blob.core.windows.net')
CONTAINER_NAME      = os.getenv('AZURE_BLOB_CONTAINER_CELONIS', 'celonis-tables')
TEST_MP_BLOB_NAME   = os.getenv('CELONIS_TEST_MP_BLOB',  'test_mp_customer_master.parquet')
SHEET3_BLOB_NAME    = os.getenv('CELONIS_SHEET3_BLOB',   'sheet3_order_mapping.parquet')
SO_RESULTS_BLOB_NAME = os.getenv('CELONIS_SO_RESULTS_BLOB', 'so_creation_results.parquet')

# ─── SO Results Table: Celonis view/table name where Action Flow writes SO outcomes ───
# This is the table Stage 2 (run_celonis_feedback.py) reads from.
# Set SO_RESULTS_TABLE in .env to match the exact Celonis table/view name.
SO_RESULTS_TABLE_NAME = os.getenv('SO_RESULTS_TABLE', 'SO_CREATION_RESULTS')

# ─── Local cache paths ────────────────────────────────────────────────────────
CACHE_DIR = Path(__file__).parent / '.celonis_cache'


def _print_section(title: str):
    print(f"\n{'='*65}")
    print(f"  {title}")
    print(f"{'='*65}")


def connect_celonis():
    """Connect to Celonis and return (celonis, data_pool, data_model, analysis)."""
    print("  Connecting to Celonis Cloud...")
    from pycelonis import get_celonis
    c = get_celonis(CELONIS_URL, API_TOKEN)
    print("  [OK] Connected.")

    print("  Locating Data Pool and Data Model...")
    data_pool  = c.data_integration.get_data_pool(POOL_ID)
    data_model = data_pool.get_data_model(DATA_MODEL_ID)

    print("  Locating Analysis...")
    space    = c.apps.get_spaces().find(SPACE_NAME)
    package  = space.get_packages().find(PACKAGE_NAME)
    analysis = package.get_analysis(ANALYSIS_ID)

    print("  [OK] Analysis found.")
    return c, data_pool, data_model, analysis


def discover_test_mp_component(analysis):
    """
    Auto-discover the first queryable table component in the 'Test MP' sheet.
    Returns the component object or None.
    """
    try:
        content = analysis.get_content()
        sheets  = content.draft.document.components
        sheet   = sheets.find(TEST_MP_NAME)
        comps   = sheet.components

        print(f"  Discovered {len(comps)} component(s) in '{TEST_MP_NAME}':")
        for comp in comps:
            comp_id   = getattr(comp, 'id', '?')
            comp_type = getattr(comp, 'type', '?')
            print(f"    - [{comp_type}] id={comp_id}")

        # If a pinned component ID is configured, use it
        if TEST_MP_COMP_ID:
            try:
                pinned = sheet.components.find_by_id(TEST_MP_COMP_ID)
                if pinned:
                    print(f"  [OK] Using pinned component id={TEST_MP_COMP_ID}")
                    return pinned
            except Exception:
                pass

        # Otherwise pick the first component that has a query
        for comp in comps:
            try:
                q = comp.get_query()
                if q:
                    print(f"  [OK] Auto-selected component id={getattr(comp, 'id', '?')} for Test MP")
                    return comp
            except Exception:
                continue

        print(f"  [WARN] No queryable component found in '{TEST_MP_NAME}'.")
        return None
    except Exception as e:
        print(f"  [WARN] Could not discover Test MP components: {e}")
        return None


def _combine_text_cols(df: pd.DataFrame, cols: list) -> pd.Series:
    """
    Concatenate multiple text columns into one, skipping blanks/nulls.
    E.g. ['MF Folien', 'GmbH & Co.', '', ''] → 'MF Folien GmbH & Co.'
    """
    result = pd.Series([''] * len(df), index=df.index)
    for col in cols:
        if col in df.columns:
            part = df[col].fillna('').astype(str).str.strip()
            # Add a space separator only when both sides are non-empty
            result = result.where(
                result == '',
                result + part.where(part != '', '')
            )
            result = result.where(
                (result != '') | (part == ''),
                part
            )
            # Append with space where both are non-empty
            both = (result != '') & (part != '')
            result[both] = result[both] + ' ' + part[both]
            only_part = (result == '') & (part != '')
            result[only_part] = part[only_part]
    return result.str.strip()


def extract_test_mp(data_model, analysis) -> pd.DataFrame:
    """
    Extract 'Test MP' sheet — Customer Master Data (replaces old New Sheet 2).

    Raw columns from Celonis:
      SoldTo, SoldToName1-4, SoldToCity1-2, SoldToCityCode, SoldToCitypCode,
      SoldToPostCode1-3, ShipTo, ShipToName1-4, ShipToCity1-2, ShipToCityCode,
      ShipToCitypCode, ShipToPostCode1-3, Material, MaterialDescription,
      CustomerMaterialNumber, CustomerMaterialDescription1,
      SalesOrg, CustomerGroup2, EmployeeResponsible, Employee Email

    Combined output columns:
      sold_to_id, sold_to_name_full, sold_to_city, sold_to_postcode,
      sold_to_city_code, ship_to_id, ship_to_name_full, ship_to_city,
      ship_to_postcode, ship_to_city_code, material_internal,
      material_description, customer_material_number,
      customer_material_description, sales_organization, customer_group2,
      salesperson_name, salesperson_email
    """
    print(f"\n  Extracting '{TEST_MP_NAME}'...")

    from pycelonis.pql.saola_connector import AnalysisSaolaConnector
    import pycelonis.pql as pql

    comp = discover_test_mp_component(analysis)
    if comp is None:
        raise RuntimeError(f"Could not find a queryable component in '{TEST_MP_NAME}'.")

    query = comp.get_query()
    print("  Running PQL query for Test MP...")
    df = pql.DataFrame.from_pql(
        query,
        saola_connector=AnalysisSaolaConnector(data_model, analysis)
    ).to_pandas()

    print(f"  Test MP fetched: {len(df)} rows, columns: {list(df.columns)}")

    # ── Step 1: Normalize all string columns ──────────────────────────────────
    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].fillna('').astype(str).str.strip()

    # ── Step 2: Combine multi-line name / city / postcode fields ─────────────
    # Celonis PQL columns arrive with a #{o_custom_CustomerRoleMaterial.XYZ} prefix.
    # Detect whether columns are already renamed (short form) or still raw (PQL form).
    _pql_prefix = '#{o_custom_CustomerRoleMaterial.'

    def _col_variants(short_names):
        """Return short names + their PQL-prefixed equivalents, keeping those present."""
        candidates = []
        for s in short_names:
            candidates.append(s)                          # short form (already renamed)
            candidates.append(_pql_prefix + s + '}')       # full PQL form (raw from Celonis)
        return candidates

    # Sold-to
    df['sold_to_name_full'] = _combine_text_cols(
        df, _col_variants(['SoldToName1', 'SoldToName2', 'SoldToName3', 'SoldToName4']))
    df['sold_to_city']      = _combine_text_cols(
        df, _col_variants(['SoldToCity1', 'SoldToCity2']))
    df['sold_to_postcode']  = _combine_text_cols(
        df, _col_variants(['SoldToPostCode1', 'SoldToPostCode2', 'SoldToPostCode3']))
    # Ship-to
    df['ship_to_name_full'] = _combine_text_cols(
        df, _col_variants(['ShipToName1', 'ShipToName2', 'ShipToName3', 'ShipToName4']))
    df['ship_to_city']      = _combine_text_cols(
        df, _col_variants(['ShipToCity1', 'ShipToCity2']))
    df['ship_to_postcode']  = _combine_text_cols(
        df, _col_variants(['ShipToPostCode1', 'ShipToPostCode2', 'ShipToPostCode3']))

    # ── Step 3: Rename individual columns to normalized names ─────────────────
    # Build the rename map for BOTH short column names (if Celonis already stripped
    # the prefix) AND the full PQL-prefixed names (#{o_custom_CustomerRoleMaterial.X}).
    _short_rename = {
        'SoldTo':                       'sold_to_id',
        'ShipTo':                       'ship_to_id',
        'SoldToCityCode':               'sold_to_city_code',
        'SoldToCitypCode':              'sold_to_cityp_code',
        'ShipToCityCode':               'ship_to_city_code',
        'ShipToCitypCode':              'ship_to_cityp_code',
        'Material':                     'material_internal',
        'MaterialDescription':          'material_description',
        'CustomerMaterialNumber':       'customer_material_number',
        'CustomerMaterialDescription1': 'customer_material_description',
        'SalesOrg':                     'sales_organization',
        'CustomerGroup2':               'customer_group2',
        'EmployeeResponsible':          'salesperson_name',
        'Employee Email':               'salesperson_email',
        'EmployeeEmail':                'salesperson_email',
    }
    rename_map = dict(_short_rename)
    # Also add PQL-prefix variants so the rename works on raw Celonis parquet columns
    _p = '#{o_custom_CustomerRoleMaterial.'
    for short, target in _short_rename.items():
        pql_key = f'{_p}{short}}}'
        rename_map[pql_key] = target

    # Only rename columns that actually exist
    rename_map = {k: v for k, v in rename_map.items() if k in df.columns}
    unmapped = [c for c in df.columns
                if c not in rename_map
                and c not in rename_map.values()
                and not c.startswith('SoldToName') and not c.startswith('ShipToName')
                and not c.startswith('SoldToCity') and not c.startswith('ShipToCity')
                and not c.startswith('SoldToPostCode') and not c.startswith('ShipToPostCode')
                and not c.startswith(f'{_p}SoldToName') and not c.startswith(f'{_p}ShipToName')
                and not c.startswith(f'{_p}SoldToCity') and not c.startswith(f'{_p}ShipToCity')
                and not c.startswith(f'{_p}SoldToPostCode') and not c.startswith(f'{_p}ShipToPostCode')
                and not c.startswith('sold_') and not c.startswith('ship_')]
    if unmapped:
        print(f"  [INFO] Unmapped columns (kept as-is): {unmapped}")
    df = df.rename(columns=rename_map)

    # ── Step 4: Drop raw multi-line columns (now replaced by combined fields) ─
    raw_cols_to_drop = [
        'SoldToName1', 'SoldToName2', 'SoldToName3', 'SoldToName4',
        'SoldToCity1', 'SoldToCity2',
        'SoldToPostCode1', 'SoldToPostCode2', 'SoldToPostCode3',
        'ShipToName1', 'ShipToName2', 'ShipToName3', 'ShipToName4',
        'ShipToCity1', 'ShipToCity2',
        'ShipToPostCode1', 'ShipToPostCode2', 'ShipToPostCode3',
        # PQL-prefixed variants (raw from Celonis)
        f'{_p}SoldToName1}}', f'{_p}SoldToName2}}', f'{_p}SoldToName3}}', f'{_p}SoldToName4}}',
        f'{_p}SoldToCity1}}', f'{_p}SoldToCity2}}',
        f'{_p}SoldToPostCode1}}', f'{_p}SoldToPostCode2}}', f'{_p}SoldToPostCode3}}',
        f'{_p}ShipToName1}}', f'{_p}ShipToName2}}', f'{_p}ShipToName3}}', f'{_p}ShipToName4}}',
        f'{_p}ShipToCity1}}', f'{_p}ShipToCity2}}',
        f'{_p}ShipToPostCode1}}', f'{_p}ShipToPostCode2}}', f'{_p}ShipToPostCode3}}',
    ]
    df = df.drop(columns=[c for c in raw_cols_to_drop if c in df.columns])

    # ── Step 5: Metadata ──────────────────────────────────────────────────────
    df['_extracted_at'] = datetime.utcnow().isoformat()
    df['_source_sheet'] = TEST_MP_NAME

    print(f"  [OK] Test MP: {len(df)} rows, columns: {list(df.columns)}")
    return df



def discover_sheet3_component(analysis):
    """
    Auto-discover the first table/chart component in New Sheet 3.
    Returns the component object or None.
    """
    try:
        content = analysis.get_content()
        sheets  = content.draft.document.components
        sheet3  = sheets.find(SHEET3_NAME)
        comps   = sheet3.components

        print(f"  Discovered {len(comps)} component(s) in '{SHEET3_NAME}':")
        for comp in comps:
            comp_id   = getattr(comp, 'id', '?')
            comp_type = getattr(comp, 'type', '?')
            print(f"    - [{comp_type}] id={comp_id}")

        # Pick the first component that has a query
        for comp in comps:
            try:
                q = comp.get_query()
                if q:
                    print(f"  [OK] Using component id={getattr(comp, 'id', '?')} for Sheet 3")
                    return comp
            except Exception:
                continue

        print("  [WARN] No queryable component found in Sheet 3.")
        return None

    except Exception as e:
        print(f"  [WARN] Could not discover Sheet 3 components: {e}")
        return None


def extract_sheet3(data_model, analysis) -> pd.DataFrame:
    """Extract New Sheet 3 — Historical Order Mapping (Customer, Material, OrderType, SalesOrg)."""
    print(f"\n  Extracting '{SHEET3_NAME}' (component: {SHEET3_COMP_ID})...")

    from pycelonis.pql.saola_connector import AnalysisSaolaConnector
    import pycelonis.pql as pql

    content = analysis.get_content()
    sheets  = content.draft.document.components
    sheet3  = sheets.find(SHEET3_NAME)
    comp    = sheet3.components.find_by_id(SHEET3_COMP_ID)
    query   = comp.get_query()

    print("  Running PQL query for Sheet 3...")
    df = pql.DataFrame.from_pql(
        query,
        saola_connector=AnalysisSaolaConnector(data_model, analysis)
    ).to_pandas()

    print(f"  Sheet 3 fetched: {len(df)} rows, columns: {list(df.columns)}")

    # ── Exact column rename using confirmed Celonis column names ──
    # These were verified by running the extractor on 2026-05-22.
    SHEET3_COL_MAP = {
        '#{o_custom_TransactionalData.Customer}':   'customer_id',
        'Material':                                  'material_internal',
        'Sales Organization':                        'sales_organization',
        '#{o_custom_TransactionalData.SalesOrder}': 'sales_order',
        '#{o_custom_TransactionalData.OrderType}':  'order_type',
        'Creation Date':                             'creation_date',
        'Customer Purchase Order':                   'customer_po_number',
    }

    rename = {k: v for k, v in SHEET3_COL_MAP.items() if k in df.columns}
    unmapped = [c for c in df.columns if c not in rename]
    if unmapped:
        print(f"  Note: unmapped columns (kept as-is): {unmapped}")

    print(f"  Renaming columns: {rename}")
    df = df.rename(columns=rename)

    # Clean nulls — iterate safely (no duplicate column names now)
    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].fillna('').astype(str).str.strip()

    # Add metadata
    df['_extracted_at'] = datetime.utcnow().isoformat()
    df['_source_sheet'] = SHEET3_NAME

    print(f"  [OK] Sheet 3: {len(df)} rows x {len(df.columns)} columns")
    return df


def extract_so_results(data_model, analysis) -> pd.DataFrame:
    """
    Extract SO creation results from the Celonis 'Touchless Sales Order' view.

    Captures BOTH success rows (SO_NUMBER populated) AND failed rows
    (Action Flow ran but BAPI_SALESORDER_CREATEFROMDAT2 returned an RFC error).

    Output columns:
      PO_NUMBER, SO_NUMBER, STATUS ('SUCCESS'/'FAILED'/'BLOCKED'),
      BLOCK_CODE, BLOCK_REASON, FAILURE_REASON (combined RFC error text),
      RFC_ERROR_CODE, RFC_MESSAGE, SALES_ORG, CUSTOMER_NAME, SOURCE_FILE,
      RUN_TIMESTAMP
    """
    print(f"\n  Extracting SO results from Celonis 'Touchless Sales Order' view...")

    try:
        from pycelonis.pql.saola_connector import AnalysisSaolaConnector
        import pycelonis.pql as pql

        print("  Querying Touchless Sales Order view (o_custom_PoExtractionResults)...")
        q = pql.PQL()
        q.add(pql.PQLColumn(
            name='PO_NUMBER',
            query='"o_custom_PoExtractionResults"."PoNumber"'
        ))
        q.add(pql.PQLColumn(
            name='SO_NUMBER',
            query='PU_FIRST("o_custom_PoExtractionResults", '
                  '"O_CUSTOM_POEXTRACTIONRESULTS_AUG_CREATED_SALES_ORDER_NUMBER"."VALUE")'
        ))
        q.add(pql.PQLColumn(
            name='SALES_ORG',
            query='"o_custom_PoExtractionResults"."SalesOrg"'
        ))
        q.add(pql.PQLColumn(
            name='CUSTOMER_NAME',
            query='"o_custom_PoExtractionResults"."CustomerName"'
        ))
        q.add(pql.PQLColumn(
            name='SOURCE_FILE',
            query='"o_custom_PoExtractionResults"."SourceFile"'
        ))

        # ── RFC / Action Flow error augmented columns ────────────────────────
        # The Celonis Action Flow writes BAPI_SALESORDER_CREATEFROMDAT2 RETURN
        # table messages into augmented columns after the SAP RFC call.
        # We probe each candidate with a minimal test query first so that only
        # confirmed-existing columns are added to the main query — this prevents
        # a "table not found" error from crashing the whole DataFrame export.
        _FAILURE_CANDIDATES = [
            ('FAILURE_REASON', 'PU_FIRST("o_custom_PoExtractionResults", '
             '"O_CUSTOM_POEXTRACTIONRESULTS_AUG_RFC_FAILURE_REASON"."VALUE")'),
            ('FAILURE_REASON', 'PU_FIRST("o_custom_PoExtractionResults", '
             '"O_CUSTOM_POEXTRACTIONRESULTS_AUG_FAILURE_REASON"."VALUE")'),
            ('FAILURE_REASON', 'PU_FIRST("o_custom_PoExtractionResults", '
             '"O_CUSTOM_POEXTRACTIONRESULTS_AUG_ERROR_MESSAGE"."VALUE")'),
            ('FAILURE_REASON', 'PU_FIRST("o_custom_PoExtractionResults", '
             '"O_CUSTOM_POEXTRACTIONRESULTS_AUG_BAPI_RETURN_MESSAGE"."VALUE")'),
        ]
        _RFC_CODE_CANDIDATES = [
            ('RFC_ERROR_CODE', 'PU_FIRST("o_custom_PoExtractionResults", '
             '"O_CUSTOM_POEXTRACTIONRESULTS_AUG_RFC_ERROR_CODE"."VALUE")'),
            ('RFC_ERROR_CODE', 'PU_FIRST("o_custom_PoExtractionResults", '
             '"O_CUSTOM_POEXTRACTIONRESULTS_AUG_BAPI_RETURN_ID"."VALUE")'),
        ]
        _RFC_MSG_CANDIDATES = [
            ('RFC_MESSAGE', 'PU_FIRST("o_custom_PoExtractionResults", '
             '"O_CUSTOM_POEXTRACTIONRESULTS_AUG_RFC_MESSAGE"."VALUE")'),
            ('RFC_MESSAGE', 'PU_FIRST("o_custom_PoExtractionResults", '
             '"O_CUSTOM_POEXTRACTIONRESULTS_AUG_BAPI_RETURN_NUMBER"."VALUE")'),
        ]
        _STATUS_CANDIDATES = [
            ('ACTION_FLOW_STATUS', 'PU_FIRST("o_custom_PoExtractionResults", '
             '"O_CUSTOM_POEXTRACTIONRESULTS_AUG_SO_CREATION_STATUS"."VALUE")'),
            ('ACTION_FLOW_STATUS', 'PU_FIRST("o_custom_PoExtractionResults", '
             '"O_CUSTOM_POEXTRACTIONRESULTS_AUG_ACTION_FLOW_STATUS"."VALUE")'),
        ]

        conn = AnalysisSaolaConnector(data_model, analysis)
        _added_cols = {}  # col_name -> True if column successfully probed & added

        def _probe_and_add(col_candidates):
            """Test each candidate with a 1-row probe query; add the first that works."""
            for col_name, pql_expr in col_candidates:
                if col_name in _added_cols:
                    return True  # already resolved
                try:
                    _probe = pql.PQL()
                    _probe.add(pql.PQLColumn(name=col_name, query=pql_expr))
                    _probe.add(pql.PQLFilter('RUNNING_TOTAL(1) <= 1'))  # limit to 1 row for speed
                    pql.DataFrame.from_pql(_probe, saola_connector=conn).to_pandas()
                    # If we reach here the column exists — add to the main query
                    q.add(pql.PQLColumn(name=col_name, query=pql_expr))
                    _added_cols[col_name] = True
                    print(f"  [INFO] RFC column resolved: {col_name} ({pql_expr[:60]}...)")
                    return True
                except Exception:
                    continue
            return False

        _probe_and_add(_FAILURE_CANDIDATES)
        _probe_and_add(_RFC_CODE_CANDIDATES)
        _probe_and_add(_RFC_MSG_CANDIDATES)
        _probe_and_add(_STATUS_CANDIDATES)

        if not _added_cols.get('FAILURE_REASON'):
            print("  [INFO] FAILURE_REASON augmented column not found in Celonis — "
                  "FAILED rows will use generic error message.")

        df = pql.DataFrame.from_pql(q, saola_connector=conn).to_pandas()

        # Normalize all columns
        for col in df.columns:
            df[col] = df[col].fillna('').astype(str).str.strip()

        print(f"  [INFO] Raw rows from Touchless view: {len(df)}")

        _EMPTY_VALS = {'', 'nan', 'None', 'NULL', 'none'}
        now_ts = datetime.utcnow().isoformat()
        out_rows = []

        for _, row in df.iterrows():
            so_num    = str(row.get('SO_NUMBER', '')).strip()
            po_num    = str(row.get('PO_NUMBER', '')).strip()
            sales_org = str(row.get('SALES_ORG', '')).strip()
            cust_name = str(row.get('CUSTOMER_NAME', '')).strip()
            src_file  = str(row.get('SOURCE_FILE', '')).strip()

            failure_reason = str(row.get('FAILURE_REASON', '')).strip()
            rfc_error_code = str(row.get('RFC_ERROR_CODE', '')).strip()
            rfc_message    = str(row.get('RFC_MESSAGE', '')).strip()
            af_status      = str(row.get('ACTION_FLOW_STATUS', '')).strip().upper()

            # Build a combined, human-readable FAILURE_REASON for the exception email
            # Format: "RFC[{code}]: {message} | {failure_reason}"
            combined_parts = []
            if rfc_error_code and rfc_error_code not in _EMPTY_VALS:
                if rfc_message and rfc_message not in _EMPTY_VALS:
                    combined_parts.append(f"RFC[{rfc_error_code}]: {rfc_message}")
                else:
                    combined_parts.append(f"RFC Error Code: {rfc_error_code}")
            if failure_reason and failure_reason not in _EMPTY_VALS:
                combined_parts.append(failure_reason)
            combined_failure = ' | '.join(combined_parts) if combined_parts else ''

            so_present = so_num and so_num not in _EMPTY_VALS

            if so_present:
                # SUCCESS (or BLOCKED — SO was created but has a block)
                if af_status in ('BLOCKED', 'BLOCK'):
                    status_val   = 'BLOCKED'
                    block_code   = rfc_error_code or 'BLOCK'
                    block_reason = combined_failure or 'Order block placed by SAP'
                else:
                    status_val   = 'SUCCESS'
                    block_code   = ''
                    block_reason = ''
                out_rows.append({
                    'PO_NUMBER':      po_num,
                    'SO_NUMBER':      so_num,
                    'STATUS':         status_val,
                    'BLOCK_CODE':     block_code,
                    'BLOCK_REASON':   block_reason,
                    'FAILURE_REASON': combined_failure,
                    'RFC_ERROR_CODE': rfc_error_code,
                    'RFC_MESSAGE':    rfc_message,
                    'SALES_ORG':      sales_org,
                    'CUSTOMER_NAME':  cust_name,
                    'SOURCE_FILE':    src_file,
                    'RUN_TIMESTAMP':  now_ts,
                })
            elif combined_failure or af_status in ('FAILED', 'ERROR', 'FAILURE'):
                # FAILED row — Action Flow ran but SAP RFC returned an error
                out_rows.append({
                    'PO_NUMBER':      po_num,
                    'SO_NUMBER':      '',
                    'STATUS':         'FAILED',
                    'BLOCK_CODE':     '',
                    'BLOCK_REASON':   '',
                    'FAILURE_REASON': combined_failure or 'SAP RFC call failed — see Celonis Action Flow logs',
                    'RFC_ERROR_CODE': rfc_error_code,
                    'RFC_MESSAGE':    rfc_message,
                    'SALES_ORG':      sales_org,
                    'CUSTOMER_NAME':  cust_name,
                    'SOURCE_FILE':    src_file,
                    'RUN_TIMESTAMP':  now_ts,
                })
            # else: row has no SO and no RFC error = not yet processed by Action Flow → skip

        if not out_rows:
            print("  [INFO] No completed SO results yet (no SUCCESS or FAILED rows).")
            return pd.DataFrame()

        df_out = pd.DataFrame(out_rows)
        df_out['_extracted_at'] = datetime.utcnow().isoformat()
        df_out['_source_table'] = 'Touchless Sales Order View (o_custom_PoExtractionResults)'

        success_cnt = len(df_out[df_out['STATUS'] == 'SUCCESS'])
        failed_cnt  = len(df_out[df_out['STATUS'] == 'FAILED'])
        blocked_cnt = len(df_out[df_out['STATUS'] == 'BLOCKED'])
        print(f"  [OK] SO results: {success_cnt} SUCCESS | {failed_cnt} FAILED | {blocked_cnt} BLOCKED")
        for _, r in df_out[df_out['STATUS'] == 'SUCCESS'].iterrows():
            print(f"       SO: {r['SO_NUMBER']}  PO: {r['PO_NUMBER']}  "
                  f"Customer: {r['CUSTOMER_NAME'][:40]}")
        for _, r in df_out[df_out['STATUS'] == 'FAILED'].iterrows():
            print(f"       FAILED PO: {r['PO_NUMBER']}  "
                  f"Reason: {r['FAILURE_REASON'][:80]}")

        return df_out

    except Exception as e:
        print(f"  [ERROR] Failed to extract Touchless SO results: {e}")
        import traceback; traceback.print_exc()
        return pd.DataFrame()


def save_local_parquet(df: pd.DataFrame, filename: str) -> Path:
    """Save DataFrame as Parquet to local cache directory."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / filename
    df.to_parquet(str(path), index=False, engine='pyarrow')
    size_kb = path.stat().st_size / 1024
    print(f"  [OK] Saved locally: {path}  ({size_kb:.1f} KB)")
    return path


def upload_to_azure_blob(local_path: Path, blob_name: str):
    """Upload a local file to the Azure Blob Storage celonis-tables container.

    Auth priority:
      1. AZURE_STORAGE_ACCOUNT_KEY (from .env) — direct key auth, no CLI login needed
      2. AZURE_STORAGE_CONNECTION_STRING (from .env) — full connection string
      3. DefaultAzureCredential — falls back to az login / managed identity
    """
    from azure.storage.blob import BlobServiceClient, ContentSettings

    print(f"  Uploading '{local_path.name}' to {AZURE_BLOB_URL}/{CONTAINER_NAME}/{blob_name} ...")

    # ── Auth: prefer Account Key (no CLI login required) ─────────────────────
    _acct_key  = os.getenv("AZURE_STORAGE_ACCOUNT_KEY", "").strip()
    _acct_name = os.getenv("AZURE_STORAGE_ACCOUNT_NAME", "").strip()
    _conn_str  = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "").strip()

    if _acct_key and _acct_name:
        # Build connection string from account name + key (works on all SDK versions)
        _built_conn = (
            f"DefaultEndpointsProtocol=https;"
            f"AccountName={_acct_name};"
            f"AccountKey={_acct_key};"
            f"EndpointSuffix=core.windows.net"
        )
        blob_service = BlobServiceClient.from_connection_string(_built_conn)
        print(f"  [AUTH] Using Storage Account Key (connection string) for '{_acct_name}'")
    elif _conn_str:
        blob_service = BlobServiceClient.from_connection_string(_conn_str)
        print(f"  [AUTH] Using Connection String")
    else:
        # Fallback: DefaultAzureCredential (requires az login with storage scope)
        from azure.identity import DefaultAzureCredential
        credential = DefaultAzureCredential()
        blob_service = BlobServiceClient(account_url=AZURE_BLOB_URL, credential=credential)
        print(f"  [AUTH] Using DefaultAzureCredential (ensure az login is current)")

    # Ensure container exists
    try:
        blob_service.create_container(CONTAINER_NAME)
        print(f"  Created container '{CONTAINER_NAME}'.")
    except Exception:
        pass  # Already exists

    blob_client = blob_service.get_blob_client(container=CONTAINER_NAME, blob=blob_name)

    with open(local_path, 'rb') as data:
        blob_client.upload_blob(
            data,
            overwrite=True,
            content_settings=ContentSettings(content_type='application/octet-stream'),
        )

    # Verify
    props = blob_client.get_blob_properties()
    size_kb = props.size / 1024
    print(f"  [OK] Upload OK: {blob_name}  ({size_kb:.1f} KB)")
    print(f"    URL: {blob_client.url}")


def run(extract_sheet2_flag=True, extract_sheet3_flag=True,
        extract_so_results_flag=True, dry_run=False):
    """Main extraction + upload routine."""
    _print_section("Celonis -> Azure Blob Storage: Table Loader")
    print(f"  Timestamp  : {datetime.utcnow().isoformat()}Z")
    print(f"  Blob URL   : {AZURE_BLOB_URL}")
    print(f"  Container  : {CONTAINER_NAME}")
    print(f"  Dry Run    : {dry_run}")

    # -- Step 1: Connect to Celonis -------------------------------------------
    _print_section("Step 1 - Connecting to Celonis")
    try:
        c, data_pool, data_model, analysis = connect_celonis()
    except Exception as e:
        print(f"\n  [ERR] ERROR connecting to Celonis: {e}")
        sys.exit(1)

    results = {}

    # -- Step 2: Extract Test MP (Customer Master) ----------------------------
    if extract_sheet2_flag:
        _print_section(f"Step 2 - Extract {TEST_MP_NAME} (Customer Master)")
        try:
            df_mp = extract_test_mp(data_model, analysis)
            local_mp = save_local_parquet(df_mp, TEST_MP_BLOB_NAME)
            results['test_mp'] = {'df': df_mp, 'local_path': local_mp, 'blob_name': TEST_MP_BLOB_NAME}
        except Exception as e:
            print(f"\n  ERROR extracting Test MP: {e}")
            import traceback; traceback.print_exc()

    # -- Step 3: Extract Sheet 3 (Historical Order Mapping) -------------------
    if extract_sheet3_flag:
        _print_section(f"Step 3 - Extract {SHEET3_NAME}")
        try:
            df3 = extract_sheet3(data_model, analysis)
            if not df3.empty:
                local3 = save_local_parquet(df3, SHEET3_BLOB_NAME)
                results['sheet3'] = {'df': df3, 'local_path': local3, 'blob_name': SHEET3_BLOB_NAME}
            else:
                print("  [WARN] Sheet 3 returned empty - skipping upload.")
        except Exception as e:
            print(f"\n  ERROR extracting Sheet 3: {e}")
            import traceback; traceback.print_exc()

    # -- Step 3b: Extract SO_CREATION_RESULTS (Stage 2 feedback table) --------
    if extract_so_results_flag:
        _print_section(f"Step 3b - Extract SO Creation Results ({SO_RESULTS_TABLE_NAME})")
        print("  This table is populated by the Celonis Action Flow after SAP SO creation.")
        print("  Stage 2 (run_celonis_feedback.py) reads this blob to route Robona / CSR emails.")
        try:
            df_so = extract_so_results(data_model, analysis)
            if not df_so.empty:
                local_so = save_local_parquet(df_so, SO_RESULTS_BLOB_NAME)
                results['so_results'] = {
                    'df': df_so,
                    'local_path': local_so,
                    'blob_name': SO_RESULTS_BLOB_NAME
                }
            else:
                print("  [WARN] SO Results table empty or not found - skipping upload.")
        except Exception as e:
            print(f"\n  ERROR extracting SO Results: {e}")
            import traceback; traceback.print_exc()

    # -- Step 4: Upload to Azure Blob -----------------------------------------
    if not dry_run:
        _print_section("Step 4 - Upload to Azure Blob Storage")
        for sheet_key, info in results.items():
            try:
                upload_to_azure_blob(info['local_path'], info['blob_name'])
            except Exception as e:
                print(f"\n  ERROR uploading {sheet_key}: {e}")
                import traceback; traceback.print_exc()
    else:
        _print_section("Step 4 - DRY RUN: Skipping Azure Upload")
        print("  Local Parquet files saved to .celonis_cache/ - not uploaded.")

    # -- Summary ---------------------------------------------------------------
    _print_section("Summary")
    for sheet_key, info in results.items():
        df = info['df']
        print(f"  {sheet_key.upper()}: {len(df)} rows x {len(df.columns)} columns")
        print(f"    Blob     : {info['blob_name']}")
        print(f"    Columns  : {list(df.columns)}")
        if len(df) > 0:
            print(f"    Sample   : {df.iloc[0].to_dict()}")
        print()

    print("\n  [OK] Celonis -> Azure load complete.\n")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Extract Celonis tables and upload to Azure Blob Storage')
    parser.add_argument('--sheet2-only', action='store_true', help='Only extract New Sheet 2')
    parser.add_argument('--sheet3-only', action='store_true', help='Only extract New Sheet 3')
    parser.add_argument('--so-results-only', action='store_true', help='Only extract SO Creation Results')
    parser.add_argument('--dry-run',     action='store_true', help='Extract locally but do NOT upload to Azure')
    args = parser.parse_args()

    if args.sheet2_only:
        run(extract_sheet2_flag=True,  extract_sheet3_flag=False, extract_so_results_flag=False, dry_run=args.dry_run)
    elif args.sheet3_only:
        run(extract_sheet2_flag=False, extract_sheet3_flag=True,  extract_so_results_flag=False, dry_run=args.dry_run)
    elif args.so_results_only:
        run(extract_sheet2_flag=False, extract_sheet3_flag=False, extract_so_results_flag=True,  dry_run=args.dry_run)
    else:
        run(extract_sheet2_flag=True,  extract_sheet3_flag=True,  extract_so_results_flag=True,  dry_run=args.dry_run)
