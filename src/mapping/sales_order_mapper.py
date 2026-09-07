"""
Sales Order Mapper
------------------
Loads the mapping table and maps extracted PO data into structured Sales Orders.

Data sources (choose one):
  A) Excel file (legacy):  SalesOrderMapper("EXPORT_20260216_100124.XLSX")
  B) Azure Blob Storage:   SalesOrderMapper.from_azure()     ← NEW

With Azure source, the mapper uses:
  - New Sheet 2 (Azure Blob) for Customer Master / material mapping
  - New Sheet 3 (Azure Blob) for historical order type lookup
  - OrderTypeDecisionTree for intelligent order type determination

Excel / Sheet 2 columns expected:
  - Sales Organization, Customer, Material, Customer Material Number,
    Order type, One SO per PO (X) / Separate SOs per PO item (O)

Usage:
    from sales_order_mapper import SalesOrderMapper
    mapper = SalesOrderMapper("EXPORT_20260216_100124.XLSX")   # Excel (legacy)
    mapper = SalesOrderMapper.from_azure()                     # Azure Blob (new)
    orders = mapper.generate_sales_orders(extracted_data, email_text=email_body)
"""

import re
import pandas as pd
from difflib import SequenceMatcher
from typing import List, Dict, Optional


CUSTOMER_ID_ALIASES = {
    "4020036720": "4020010504",  # Fuyu Electronical Technology
}

# ---------------------------------------------------------------------------
# Unit of Measure normalisation — maps non-standard / regional UoM codes
# to SAP-standard codes so all POs use a consistent unit.
# KGM is ISO 3-letter code for kilogram; SAP uses KG.
#
# IMPERIAL → METRIC conversion:
#   SAP only accepts metric units (KG, G, T, M, etc.).
#   When a PO uses LBS/OZ/etc. we auto-convert the quantity to KG
#   and update the unit accordingly before writing to SAP.
#
# METRIC TONNE handling:
#   POs sometimes use 'mt', 'MT', 'mton', 'tonne' to mean metric tonne.
#   1 metric tonne = 1000 KG.  We convert to KG so SAP receives a
#   consistent unit (KG).
#
# US SHORT TON (T in US/AMS context):
#   In US/Americas POs the bare unit 'T' typically means US short ton
#   (2000 lb = 907.184740 kg).  When is_us_format=True we convert qty × 907.18
#   and emit unit KG.  Outside of US context, 'T' is passed through as the
#   SAP metric-tonne code (T) with no quantity change.
# ---------------------------------------------------------------------------
UNIT_NORMALISATION = {
    # ── ISO 3-letter → SAP codes (no qty change) ───────────────────────────
    "KGM": "KG",   # ISO kilogram       → SAP kilogram
    "KGS": "KG",   # kilogrammes (alt)  → SAP kilogram
    "MTR": "M",    # ISO metre          → SAP metre
    "MTQ": "M3",   # cubic metre        → SAP cubic metre
    "LTR": "L",    # litre              → SAP litre
    "GRM": "G",    # gram               → SAP gram
    "TNE": "T",    # tonne              → SAP tonne
    "PCE": "PC",   # piece              → SAP piece
    "SET": "ST",   # set                → SAP set
    # ── Imperial → SAP codes (no qty change; SAP accepts LB directly) ─────
    "LB":  "LB",   # pound
    "LBS": "LB",   # pounds
    "LBM": "LB",   # pound-mass
    # ── German / European / Dutch full-word kilogram spellings ───────────
    "KILOGRAMM":   "KG",   # German: Kilogramm          → SAP kilogram
    "KILOGRAMME":  "KG",   # French/British: kilogramme → SAP kilogram
    "KILOGRAMMES": "KG",   # French/British plural       → SAP kilogram
    "KILOGRAM":    "KG",   # English full word           → SAP kilogram
    "KILOGRAMS":   "KG",   # English plural              → SAP kilogram
    "KILOGRAAM":   "KG",   # Dutch full word             → SAP kilogram
    "KILOGRAAMS":  "KG",   # Dutch plural                → SAP kilogram
    "KILOGRAMEN":  "KG",   # Dutch alt plural            → SAP kilogram
    "KILOGRAAMEN": "KG",   # Dutch alt plural            → SAP kilogram
    "KILO":        "KG",   # informal abbreviation       → SAP kilogram
    "KILOS":       "KG",   # informal plural             → SAP kilogram
    "KG":          "KG",   # SAP standard kilogram
    "KGS":         "KG",   # alt plural abbreviation     → SAP kilogram
    "KGM":         "KG",   # ISO abbreviation            → SAP kilogram
    "KILOGRAMA":   "KG",   # Spanish/Portuguese          → SAP kilogram
    "KILOGRAMAS":  "KG",   # Spanish/Portuguese plural   → SAP kilogram
    "CHILOGRAMMO": "KG",   # Italian                     → SAP kilogram
    "CHILOGRAMMI": "KG",   # Italian plural              → SAP kilogram
}

# Units that require quantity conversion to KG before sending to SAP.
# Format: raw_unit_upper → (sap_unit, factor_to_kg)
#   quantity_kg = quantity_original * factor
IMPERIAL_TO_KG = {
    "OZ":  ("KG", 0.02834952),   # ounce
    "OZM": ("KG", 0.02834952),   # ounce-mass (ISO)
    "TON": ("KG", 907.184740),   # US short ton (2000 lb)
    "STON":("KG", 907.184740),   # short ton alt
    "LTON":("KG", 1016.04691),   # long ton (UK, 2240 lb)
}

# Metric-tonne aliases → always convert to KG (1 mt = 1000 KG).
# These are used when the PO explicitly spells out 'mt', 'mton', 'tonne' etc.
# They are unambiguous regardless of region.
METRIC_TONNE_TO_KG = {
    "MT":    ("KG", 1000.0),   # metric tonne (most common alias)
    "MTON":  ("KG", 1000.0),   # metric ton   (alt spelling)
    "TONNE": ("KG", 1000.0),   # tonne (full word)
    "MTS":   ("KG", 1000.0),   # metric tonnes (plural)
    # ── Chinese / CJK metric tonne characters ──────────────────────────────
    "吨":    ("KG", 1000.0),   # Chinese: 吨 (metric tonne, Simplified)
    "噸":    ("KG", 1000.0),   # Chinese: 噸 (metric tonne, Traditional)
    "公吨":  ("KG", 1000.0),   # Chinese: 公吨 (metric tonne, full form Simplified)
    "公噸":  ("KG", 1000.0),   # Chinese: 公噸 (metric tonne, full form Traditional)
}


def _normalise_unit(raw_unit: str, is_us_format: bool = False) -> str:
    """Normalise a raw UoM string to its SAP-standard equivalent (unit only, no qty change)."""
    if not raw_unit or str(raw_unit).strip().lower() in ("nan", "none", ""):
        return "KG"
    upper = str(raw_unit).strip().upper().rstrip(".")
    if upper in METRIC_TONNE_TO_KG:
        return METRIC_TONNE_TO_KG[upper][0]   # KG
    if upper in IMPERIAL_TO_KG:
        return IMPERIAL_TO_KG[upper][0]        # KG
    # Bare 'T' in US context → short ton → KG; otherwise pass through as SAP tonne
    if upper == "T" and is_us_format:
        return "KG"
    return UNIT_NORMALISATION.get(upper, upper)


def _normalise_unit_and_qty(raw_unit: str, raw_qty, is_us_format: bool = False):
    """
    Normalise a UoM + quantity pair for SAP writeback.

    Returns (sap_unit: str, converted_qty: str)

    Conversion rules (applied in this order):
      1. Metric-tonne aliases (MT, mton, tonne, …) → KG  (×1000)
         These are always metric tonnes, regardless of region.
      2. Imperial → KG (LBS, OZ, TON, STON, LTON) → KG  (×factor)
      3. Bare 'T' in US/AMS context (is_us_format=True) → KG  (×907.184740)
         In non-US context 'T' is already the SAP metric-tonne code; passed through.
      4. All other units: only normalise the code string (no qty change).

    If the unit is not available (empty/missing), it defaults to 'KG'.
    """
    if not raw_unit or str(raw_unit).strip().lower() in ("nan", "none", ""):
        return "KG", str(raw_qty) if raw_qty is not None else ""

    upper = str(raw_unit).strip().upper().rstrip(".")

    def _apply_factor(sap_unit, factor):
        """Multiply raw_qty by factor and return (sap_unit, qty_str)."""
        try:
            qty_num = float(str(raw_qty).replace(",", "").strip())
            converted = round(qty_num * factor, 3)
            converted_str = f"{converted:.3f}".rstrip("0").rstrip(".")
            print(f"  [UnitConv] {raw_qty} {upper} → {converted_str} {sap_unit}  (×{factor})")
            return sap_unit, converted_str
        except (ValueError, TypeError):
            return sap_unit, str(raw_qty) if raw_qty is not None else ""

    # ── 1. Metric-tonne aliases → KG (×1000) ──────────────────────────────
    if upper in METRIC_TONNE_TO_KG:
        sap_unit, factor = METRIC_TONNE_TO_KG[upper]
        return _apply_factor(sap_unit, factor)

    # ── 2. Imperial → KG conversion ───────────────────────────────────────
    if upper in IMPERIAL_TO_KG:
        sap_unit, factor = IMPERIAL_TO_KG[upper]
        return _apply_factor(sap_unit, factor)

    # ── 3. Bare 'T' in US context → US short ton → KG ─────────────────────
    #    Outside US context 'T' is the SAP metric-tonne code; pass through.
    if upper == "T" and is_us_format:
        return _apply_factor("KG", 907.184740)

    # ── 4. ISO / alias normalisation (no qty change) ──────────────────────
    sap_unit = UNIT_NORMALISATION.get(upper, upper)
    # ── 4. ISO / alias normalisation (no qty change) ──────────────────────
    sap_unit = UNIT_NORMALISATION.get(upper, upper)
    return sap_unit, str(raw_qty) if raw_qty is not None else ""
    # NOTE: dead code block that previously existed here (lines 186-214) was
    # removed — it was an unreachable duplicate of the function body above,
    # left by a copy-paste error. The live logic at lines 156-184 is unchanged.




class SalesOrderMapper:
    def __init__(self, excel_path: str):
        """Load the Excel mapping file into memory (legacy Excel mode)."""
        # Decision tree is None in Excel mode; set by from_azure() factory.
        self._decision_tree = None
        self._df2_raw = None   # Raw Sheet 2 DataFrame for customer_group2 lookups (Azure mode only)

        print(f"  [Mapper] Loading Excel mapping (legacy local mode): {excel_path}")
        print(f"  [Mapper] NOTE: Production pipeline uses Azure Blob + Decision Tree via reenrich_results.py")
        self.df = pd.read_excel(excel_path, dtype=str)
        # Normalize column names (strip whitespace, lowercase for matching)
        self.df.columns = [c.strip() for c in self.df.columns]

        # Identify columns
        self.col_sales_org   = self._find_col(["Sales Organization", "Sales Org", "SalesOrg"])
        self.col_customer    = self._find_col(["Customer"])
        self.col_material    = self._find_col(["Material"])
        self.col_cust_mat    = self._find_col(["Customer Material Number", "Customer Material", "Cust Mat"])
        self.col_mat_desc    = self._find_col(["Material Description", "MaterialDescription", "Description", "material_description"])
        self.col_order_type  = self._find_col(["Order type", "Order Type", "OrderType"])
        self.col_so_split    = self._find_col([
            "One SO per PO (X)\nSeparate SOs per PO item (O)",
            "One SO per PO",
            "SO Split",
        ])

        # Normalize data columns once for ultra-fast lookups
        for col in [self.col_customer, self.col_material, self.col_cust_mat, self.col_mat_desc]:
            if col and col in self.df.columns:
                self.df[col] = self.df[col].fillna("").astype(str).str.strip()

        print(f"  [Mapper] Loaded {len(self.df)} mapping rows.")
        print(f"  [Mapper] Columns found: SalesOrg={self.col_sales_org}, Customer={self.col_customer}, "
              f"Material={self.col_material}, CustMat={self.col_cust_mat}, "
              f"OrderType={self.col_order_type}, SOSplit={self.col_so_split}")

    def _find_col(self, candidates: List[str]) -> Optional[str]:
        """Find the first matching column name from candidates."""
        for c in candidates:
            if c in self.df.columns:
                return c
            # Try case-insensitive
            for dc in self.df.columns:
                if dc.strip().lower() == c.strip().lower():
                    return dc
        # Fallback: partial match
        for c in candidates:
            for dc in self.df.columns:
                if c.lower() in dc.lower():
                    return dc
        return None

    # --- Azure factory -------------------------------------------------

    @classmethod
    def from_azure(cls, force_refresh: bool = True) -> 'SalesOrderMapper':
        """
        Build a SalesOrderMapper backed by Azure Blob Storage tables.

        Loads Sheet 2 (Customer Master) and Sheet 3 (Order History) from
        the 'celonis-tables' Azure Blob container and wires up the
        OrderTypeDecisionTree for intelligent order type determination.

        Args:
            force_refresh: If True, bypass local cache and re-download.

        Returns:
            SalesOrderMapper instance with decision tree enabled.
        """
        print("  [Mapper] Loading tables from Azure Blob Storage (Test MP + Sheet 3)...")
        from azure_table_reader import AzureTableReader
        from order_type_decision_tree import OrderTypeDecisionTree

        # force_refresh=True: always download the latest parquet from Azure Blob.
        # celonis_to_azure.py runs first (Step -1 in the pipeline) and pushes
        # freshly fetched Celonis data, so we must bypass the 24h local cache here.
        reader = AzureTableReader(force_refresh=force_refresh)
        df_mp  = reader.get_test_mp()  # Customer Master (Test MP — replaces old Sheet 2)
        df3    = reader.get_sheet3()   # Order History  (Sheet 3 — unchanged)

        # Build the decision tree
        # Sheet 3 uses customer_id, material_internal, sales_organization, order_type
        # Pass df_mp as the sheet2-equivalent for any decision tree logic that needs it
        tree = OrderTypeDecisionTree(df_sheet2=df_mp, df_sheet3=df3)

        # Build mapping DataFrame from Test MP
        # sold_to_id = the SAP customer/sold-to number — this is the primary customer key
        df_map = df_mp.rename(columns={
            'sold_to_id':              'Customer',
            'material_internal':       'Material',
            'customer_material_number':'Customer Material Number',
            'sales_organization':      'Sales Organization',
            # No order_type in Test MP — comes from Sheet 3 / decision tree
        })

        # Use the internal constructor to set all attributes properly
        instance = cls.__new__(cls)
        instance._decision_tree   = tree
        instance._df2_raw         = df_mp   # kept as _df2_raw for backward compat
        instance._df_mp_raw       = df_mp   # canonical name for Test MP raw data
        instance.df               = df_map
        instance.df.columns       = [c.strip() for c in instance.df.columns]
        instance.col_sales_org    = instance._find_col(["Sales Organization"])
        instance.col_customer     = instance._find_col(["Customer"])
        instance.col_material     = instance._find_col(["Material"])
        instance.col_cust_mat     = instance._find_col(["Customer Material Number"])
        instance.col_mat_desc     = instance._find_col(
            ["material_description", "Material Description", "Description"])
        instance.col_order_type   = instance._find_col(["Order type"])
        instance.col_so_split     = instance._find_col(["One SO per PO", "SO Split"])
        # New: Test MP-specific disambiguation columns (kept in df_map after rename)
        instance.col_sold_to_name = instance._find_col(["sold_to_name_full"])
        instance.col_sold_to_city = instance._find_col(["sold_to_city"])
        instance.col_sold_to_post = instance._find_col(["sold_to_postcode"])
        instance.col_ship_to_id   = instance._find_col(["ship_to_id"])
        instance.col_ship_to_name = instance._find_col(["ship_to_name_full"])

        # Normalize data columns once for ultra-fast lookups
        for col in [
            instance.col_customer, instance.col_material,
            instance.col_cust_mat, instance.col_mat_desc,
            instance.col_sold_to_name, instance.col_sold_to_city,
            instance.col_sold_to_post,
        ]:
            if col and col in instance.df.columns:
                instance.df[col] = instance.df[col].fillna("").astype(str).str.strip()

        # Normalize city/postcode in raw Test MP for location-based disambiguation
        for loc_col in ['sold_to_city', 'sold_to_postcode', 'ship_to_city']:
            if loc_col in df_mp.columns:
                df_mp[loc_col] = df_mp[loc_col].fillna('').astype(str).str.strip().str.lower()

        print(f"  [Mapper] Test MP mode: {len(df_map)} mapping rows, decision tree active.")
        return instance

    # ------------------------------------------------------------------
    # Vendor / Ship-to helpers (replace SQLite knowledge_base.db calls)
    # ------------------------------------------------------------------

    def get_vendor_list(self) -> list:
        """
        Return a list of (customer_id, customer_name) tuples for fuzzy vendor matching.

        In Azure mode: reads directly from the Test MP parquet (_df_mp_raw).
        In Excel mode: falls back to scanning the mapping DataFrame.

        This replaces:
            SELECT DISTINCT customer_number, name FROM vendors
        """
        # Azure mode — use the raw Test MP which has one row per sold_to × material
        raw = getattr(self, '_df_mp_raw', None)
        if raw is not None:
            pairs = (
                raw[['sold_to_id', 'sold_to_name_full']]
                .drop_duplicates()
                .dropna(subset=['sold_to_id', 'sold_to_name_full'])
            )
            pairs = pairs[
                (pairs['sold_to_id'].astype(str).str.strip() != '') &
                (pairs['sold_to_name_full'].astype(str).str.strip() != '')
            ]
            result = list(zip(
                pairs['sold_to_id'].astype(str).str.strip(),
                pairs['sold_to_name_full'].astype(str).str.strip(),
            ))
            print(f"  [KB] Loaded {len(result)} vendors from Test MP (Azure Blob).")
            return result

        # Excel mode fallback — extract from mapping DataFrame
        if self.col_customer and self.col_sold_to_name and self.col_sold_to_name in self.df.columns:
            pairs = (
                self.df[[self.col_customer, self.col_sold_to_name]]
                .drop_duplicates()
                .dropna()
            )
            result = [(r[0], r[1]) for r in pairs.itertuples(index=False) if r[0] and r[1]]
            print(f"  [KB] Loaded {len(result)} vendors from Excel mapping.")
            return result

        print("  [KB] Warning: no vendor data available (no Test MP and no Excel sold_to_name).")
        return []

    def get_materials_for_customer(self, customer_id: str) -> set:
        """
        Return a set of all customer material numbers + internal material numbers
        for a given customer ID.

        Used for material-overlap disambiguation when multiple vendor names fuzzy-tie.

        This replaces:
            SELECT customer_material_number, internal_material_number FROM materials
            WHERE customer_number = :cust
        """
        cust = str(customer_id).strip()
        if not cust or self.col_customer not in self.df.columns:
            return set()

        cust_rows = self.df[self.df[self.col_customer] == cust]
        mats = set()
        if self.col_cust_mat and self.col_cust_mat in cust_rows.columns:
            mats.update(
                v.strip().lower()
                for v in cust_rows[self.col_cust_mat].dropna().astype(str)
                if v.strip()
            )
        if self.col_material and self.col_material in cust_rows.columns:
            mats.update(
                v.strip().lower()
                for v in cust_rows[self.col_material].dropna().astype(str)
                if v.strip()
            )
        return mats

    def get_ship_to_rows(self, customer_id: str) -> list:
        """
        Return a list of (sold_to_id, ship_to_id, ship_to_name) tuples for a customer.

        In Azure mode: reads from _df_mp_raw which has ship_to_id + ship_to_name_full
        (normalized by _normalize_test_mp() in azure_table_reader.py).
        In Excel mode: returns an empty list (Excel file has no ship-to data).

        This replaces:
            SELECT sold_to, ship_to, ship_to_name FROM vendors WHERE customer_number = :vid
        """
        cust = str(customer_id).strip()
        raw = getattr(self, '_df_mp_raw', None)
        if raw is None or not cust:
            return []

        # Column names after _normalize_test_mp() normalization:
        #   '#{o_custom_CustomerRoleMaterial.ShipTo}' → 'ship_to_id'
        #   ship_to_name_full → already computed by _normalize_test_mp()
        ship_to_col = 'ship_to_id'
        ship_nm_col = 'ship_to_name_full'

        if ship_to_col not in raw.columns or ship_nm_col not in raw.columns:
            return []

        subset = raw[raw['sold_to_id'].astype(str).str.strip() == cust]
        if subset.empty:
            return []

        pairs = (
            subset[['sold_to_id', ship_to_col, ship_nm_col]]
            .drop_duplicates()
            .fillna('')
        )
        result = []
        for _, row in pairs.iterrows():
            result.append((
                str(row['sold_to_id']).strip(),
                str(row[ship_to_col]).strip(),
                str(row[ship_nm_col]).strip(),
            ))
        return result

    def lookup(self, customer_number: str, material_code: str, material_description: str = "", packaging: str = "") -> Optional[Dict]:
        """
        Look up a Customer + Material combination in the Excel mapping.
        Returns: dict with sales_org, order_type, customer_material, so_split_rule
        """
        if not customer_number:
            return None

        # Clean inputs
        cust = str(customer_number).strip()
        if cust in CUSTOMER_ID_ALIASES:
            cust = CUSTOMER_ID_ALIASES[cust]
        
        # Remove parenthesized comments e.g., "Pocan B7375 000000 (Natural)" -> "Pocan B7375 000000"
        import re
        mat = str(material_code).strip()
        mat = re.sub(r'\s*\([^)]*\)', '', mat).strip()
        # Normalize slash direction: Japanese POs use TW241F10/00001, master data has TW241F10\00001
        # Also normalize to forward slash for consistent matching (both directions stored)
        mat_slash_norm = mat.replace('/', '\\')   # forward → backslash (master data format)
        mat_slash_norm2 = mat.replace('\\', '/')  # backslash → forward slash (PO format)

        # ── Keep ORIGINAL material code before color stripping ────────────────
        # Some customer material codes include color as part of product identity
        # (e.g. INABATA 'TUFBET BGF30 BLACK'). Try original code first.
        mat_orig = mat
        mat_orig_slash_norm  = mat_orig.replace('/', '\\')
        mat_orig_slash_norm2 = mat_orig.replace('\\', '/')

        # Strip common color words case-insensitively with word boundaries
        mat = re.sub(r'(?i)\b(natural|nat|black|blk|white|wht|grey|gray|red|blue|green|yellow|orange|brown)\b', '', mat).strip()

        # Clean material description identically
        material_description_cleaned = str(material_description).strip()
        if material_description_cleaned:
            material_description_cleaned = re.sub(r'\s*\([^)]*\)', '', material_description_cleaned).strip()
            material_description_cleaned = re.sub(r'(?i)\b(natural|nat|black|blk|white|wht|grey|gray|red|blue|green|yellow|orange|brown)\b', '', material_description_cleaned).strip()

        # Strip Envalior product brand prefixes that LLMs sometimes include in the
        # extracted material code (e.g. 'Pocan B3235 010003' → 'B3235 010003').
        _BRAND_PREFIXES = (
            "pocan ", "stanyl ", "akulon ", "arnitel ", "xytron ", "ecopaxx ", "arnite ",
            "ertalon ", "econamid ", "nylatron ", "ketron ", "sustamid ",
            "durethan ", "tepex ", "adivant ", "novamid ",
            "ノバミット ", "ノバミット", "ノバミッド ", "ノバミッド",
            "ポカン ", "ポカン", "スタニル ", "スタニル",
            "アキュロン ", "アキュロン", "アーニテル ", "アーニテル",
            "デュラネックス ", "デュラネックス",
            "pa410チップ ", "pa410チップ", "pa410ﾁｯﾌﾟ ", "pa410ﾁｯﾌﾟ", "pa410 ", "pa410",
            "pa66チップ ", "pa66チップ", "pa66ﾁｯﾌﾟ ", "pa66ﾁｯﾌﾟ", "pa66 ", "pa66",
            "pa6チップ ", "pa6チップ", "pa6ﾁｯﾌﾟ ", "pa6ﾁｯﾌﾟ", "pa6 ", "pa6",
            "ナイロン ", "ナイロン", "ポリアミド ", "ポリアミド", "polyamide ", "polyamide",
            "樹脂チップ ", "樹脂 ",
        )
        mat_lower_check = mat.lower()
        for _bp in _BRAND_PREFIXES:
            if mat_lower_check.startswith(_bp):
                mat = mat[len(_bp):].strip()
                break

        # Pre-filter rows for this customer to drastically speed up downstream masks
        cust_df = self.df[self.df[self.col_customer] == cust]
        if cust_df.empty:
            return None

        # ── Pass 0: Exact match using ORIGINAL unstripped material code ────────
        matches = pd.DataFrame()
        if self.col_cust_mat and mat_orig:
            for _m0 in [mat_orig, mat_orig_slash_norm, mat_orig_slash_norm2]:
                _mask0 = cust_df[self.col_cust_mat] == _m0
                if not _mask0.any():
                    _mask0 = cust_df[self.col_cust_mat].str.lower() == _m0.lower()
                if _mask0.any():
                    matches = cust_df[_mask0]
                    break

        if matches.empty and mat_orig:
            _mask0m = cust_df[self.col_material] == mat_orig
            if not _mask0m.any():
                _mask0m = cust_df[self.col_material].str.lstrip("0") == mat_orig.lstrip("0")
            if _mask0m.any():
                matches = cust_df[_mask0m]

        # Exact match on internal Material ID (color-stripped mat)
        if matches.empty:
            mask = cust_df[self.col_material] == mat
            matches = cust_df[mask]

        if matches.empty:
            # Try matching material without leading zeros
            mat_stripped = mat.lstrip("0")
            mask2 = cust_df[self.col_material].str.lstrip("0") == mat_stripped
            matches = cust_df[mask2]

        # Try matching against Customer Material Number (exact, color-stripped)
        if matches.empty and self.col_cust_mat and mat:
            mask3 = cust_df[self.col_cust_mat] == mat
            if not mask3.any():
                mask3 = cust_df[self.col_cust_mat] == mat_slash_norm    # fwd→back slash
            if not mask3.any():
                mask3 = cust_df[self.col_cust_mat] == mat_slash_norm2   # back→fwd slash
            matches = cust_df[mask3]

        # Try case-insensitive match on Customer Material Number (color-stripped)
        if matches.empty and self.col_cust_mat and mat:
            mat_lower = mat.lower()
            mask4 = cust_df[self.col_cust_mat].str.lower() == mat_lower
            if not mask4.any():
                mask4 = cust_df[self.col_cust_mat].str.lower() == mat_slash_norm.lower()
            if not mask4.any():
                mask4 = cust_df[self.col_cust_mat].str.lower() == mat_slash_norm2.lower()
            matches = cust_df[mask4]

        # Try startswith match on Customer Material Number
        # e.g. extracted '111102' should match Excel '111102 OKTABIN'
        if matches.empty and self.col_cust_mat and mat:
            mat_lower = mat.lower()
            mask5 = cust_df[self.col_cust_mat].str.lower().str.startswith(mat_lower)
            matches = cust_df[mask5]

        # Reverse prefix match: extracted '11110201' should match Excel '111102 OKTABIN'
        # Extract numeric prefix from Excel Customer Material, then check if extracted code starts with it
        if matches.empty and self.col_cust_mat and mat and mat[0].isdigit():
            import re as _re
            for idx, row in cust_df.iterrows():
                excel_cm = str(row[self.col_cust_mat]).strip()
                # Extract numeric prefix from Excel value (e.g. '111102' from '111102 OKTABIN')
                m = _re.match(r'(\d+)', excel_cm)
                if m:
                    excel_prefix = m.group(1)
                    if len(excel_prefix) >= 4 and mat.startswith(excel_prefix):
                        matches = cust_df.loc[[idx]]
                        break
        # Try substring contains match on Customer Material Number (either way)
        if matches.empty and self.col_cust_mat and mat:
            mat_lower = mat.lower()
            if len(mat_lower) >= 3:
                for idx, row in cust_df.iterrows():
                    excel_cm = str(row[self.col_cust_mat]).strip().lower()
                    if excel_cm and (mat_lower in excel_cm or excel_cm in mat_lower):
                        matches = cust_df.loc[[idx]]
                        break

        # Try fuzzy match on Customer Material Number (to handle minor typos/differences)
        # Bypass fuzzy matching for purely numeric codes (different digits = completely different parts, not typos)
        is_numeric = "".join(c for c in mat if c.isalnum()).isdigit()
        if matches.empty and self.col_cust_mat and mat and len(mat) >= 6 and not is_numeric:
            from difflib import SequenceMatcher
            best_idx = None
            best_ratio = 0.0
            for idx, row in cust_df.iterrows():
                excel_cm = str(row[self.col_cust_mat]).strip()
                if abs(len(excel_cm) - len(mat)) > 4:
                    continue
                ratio = SequenceMatcher(None, mat.lower(), excel_cm.lower()).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_idx = idx
            if best_ratio >= 0.82 and best_idx is not None:
                matches = cust_df.loc[[best_idx]]

        # Try normalized alphanumeric containment match:
        # e.g., extracted mat 'ARNITEL EM 630 H' contains Excel Customer Material 'EM630-H'
        if matches.empty and self.col_cust_mat and mat:
            def clean_code(s: str) -> str:
                return "".join(c for c in str(s) if c.isalnum()).lower()
            
            clean_mat = clean_code(mat)
            if len(clean_mat) >= 3:
                for idx, row in cust_df.iterrows():
                    excel_cm = str(row[self.col_cust_mat]).strip()
                    clean_excel = clean_code(excel_cm)
                    if len(clean_excel) >= 3:
                        if clean_excel in clean_mat or clean_mat in clean_excel:
                            matches = cust_df.loc[[idx]]
                            break

        # Try normalized alphanumeric containment match against database Material Description:
        # e.g., extracted mat code 'NA99001/71104' matches database description 'K-FG0\NA99001\71104'
        if matches.empty and self.col_mat_desc and mat:
            def clean_code(s: str) -> str:
                return "".join(c for c in str(s) if c.isalnum()).lower()
            
            clean_mat = clean_code(mat)
            if len(clean_mat) >= 3:
                for idx, row in cust_df.iterrows():
                    ref_desc = str(row[self.col_mat_desc]).strip()
                    clean_ref_desc = clean_code(ref_desc)
                    if len(clean_ref_desc) >= 3:
                        if clean_ref_desc in clean_mat or clean_mat in clean_ref_desc:
                            matches = cust_df.loc[[idx]]
                            break

        # Also try matching extracted Material Description against database Material Description:
        # e.g., extracted description 'Akulon K-FG0-FC Ultraflow nat (PA6)' matches database description 'K-FG0\NA99001\71104'
        # e.g., 'POCAN BF4239-700350 S103 (ARNITE TV4261SF KN.01.74.)' matches 'BF4239 700350 S103 BC100N'
        if matches.empty and self.col_mat_desc and material_description_cleaned:
            def _get_grade_tokens(s: str) -> set:
                """Extract core grade/spec tokens by stripping parentheses and brand prefixes."""
                s_clean = re.sub(r'\s*\([^)]*\)', '', str(s)).strip()
                brands = ["pocan", "durethan", "stanyl", "akulon", "arnitel", "xytron", "tepex", "ertalon", "ecopaxx", "arnite"]
                s_lower = s_clean.lower()
                for b in brands:
                    if s_lower.startswith(b):
                        s_clean = s_clean[len(b):].strip()
                        break
                # Normalize 'B 4215' -> 'B4215', 'BF 4239' -> 'BF4239'
                s_clean = re.sub(r'\b([A-Z]+)\s+(\d+)\b', r'\1\2', s_clean, flags=re.IGNORECASE)
                tokens = set(re.sub(r'[^a-z0-9]', ' ', s_clean.lower()).split())
                stop = {'the', 'and', 'for', 'of', 'in', 'to', 'a', 'an', 'or', 'na', 'pa', 'kg', 'pos013', 's013'}
                return tokens - stop

            ext_tokens = _get_grade_tokens(material_description_cleaned)

            def clean_code(s: str) -> str:
                return "".join(c for c in str(s) if c.isalnum()).lower()
            
            clean_ext_desc = clean_code(material_description_cleaned)
            for idx, row in cust_df.iterrows():
                ref_desc = str(row[self.col_mat_desc]).strip()
                clean_ref_desc = clean_code(ref_desc)
                # Check 1: direct substring
                if len(clean_ref_desc) >= 3 and len(clean_ext_desc) >= 3:
                    if clean_ref_desc in clean_ext_desc or clean_ext_desc in clean_ref_desc:
                        matches = cust_df.loc[[idx]]
                        break
                # Check 2: token subset match (e.g. ['bf4239', '700350'] in ref_desc)
                # Also checks reverse ratio: most of the DB's spec tokens appear in the PO description.
                # This handles cases where the LLM extracts a longer description than before
                # (e.g. 'PA6 XF D GW 7040 - DURETHAN B31SK 702169' → 7 tokens, DB has 3).
                # Forward ratio: 2/7=28% fails, but reverse ratio: 2/3=67% ≥ 0.65 → match.
                if ext_tokens and len(ext_tokens) >= 2:
                    ref_tokens = set(re.sub(r'[^a-z0-9]', ' ', ref_desc.lower()).split())
                    overlap = ext_tokens & ref_tokens
                    forward_ratio = len(overlap) / len(ext_tokens)  # overlap / PO tokens
                    reverse_ratio = len(overlap) / len(ref_tokens) if ref_tokens else 0  # overlap / DB tokens
                    if (
                        ext_tokens.issubset(ref_tokens)
                        or forward_ratio >= 0.75
                        or (len(ref_tokens) >= 2 and reverse_ratio >= 0.65)
                    ):
                        matches = cust_df.loc[[idx]]
                        print(
                            f"    [Mapper] Description grade-token match: PO desc '{material_description_cleaned[:40]}' "
                            f"matched DB desc '{ref_desc}' "
                            f"(tokens={overlap}, fwd={forward_ratio:.2f}, rev={reverse_ratio:.2f})"
                        )
                        break

        # Try matching extracted Material Description against database Customer Material Number:
        # e.g., extracted description 'Akulon F130-C2' matches database customer material 'AKULON F130-C2'
        if matches.empty and self.col_cust_mat and material_description_cleaned:
            def clean_code(s: str) -> str:
                return "".join(c for c in str(s) if c.isalnum()).lower()
            
            clean_ext_desc = clean_code(material_description_cleaned)
            if len(clean_ext_desc) >= 3:
                for idx, row in cust_df.iterrows():
                    ref_cust_mat = str(row[self.col_cust_mat]).strip()
                    clean_ref_cust_mat = clean_code(ref_cust_mat)
                    if len(clean_ref_cust_mat) >= 3:
                        if clean_ref_cust_mat in clean_ext_desc or clean_ext_desc in clean_ref_cust_mat:
                            matches = cust_df.loc[[idx]]
                            break


        # ── CMIR Description-in-Material-Field matching ──────────────────────
        # Some customers (e.g. Valeo Samsung Thermal Systems) put a description
        # fragment in the CMIR "customer_material_number" field instead of a real
        # part code.  Example CMIR record:
        #   customer_material_number = "RESIN, AKULON AKV25F30 703126,DARKG"
        # When the PO arrives with:
        #   material_code = "230017221" (their own internal number — not in CMIR)
        #   material_description = "Resin,PA" or spec = "LANXESS DURETHAN BKV30Q20"
        # We detect this case by checking if the CMIR customer_material_number looks
        # like a description (contains spaces, commas, or alphabetic grade tokens)
        # and then do word-overlap scoring against the PO description + code.
        if matches.empty and self.col_cust_mat and material_description_cleaned:
            def _is_desc_fragment(cmat: str) -> bool:
                """Returns True if the CMIR customer_material_number looks like a description."""
                if not cmat or len(cmat) < 5:
                    return False
                # Descriptions have spaces and alphabetic words; codes are short alphanumeric tokens
                has_space = ' ' in cmat or ',' in cmat
                alpha_ratio = sum(1 for c in cmat if c.isalpha()) / max(len(cmat), 1)
                return has_space and alpha_ratio > 0.25

            def _word_overlap(a: str, b: str) -> float:
                """Fraction of words from shorter string found in longer string."""
                tokens_a = set(re.sub(r'[^a-z0-9]', ' ', a.lower()).split())
                tokens_b = set(re.sub(r'[^a-z0-9]', ' ', b.lower()).split())
                # Remove very short tokens and common stop words
                stop = {'the', 'and', 'for', 'of', 'in', 'to', 'a', 'an', 'or', 'na', 'pa', 'kg'}
                tokens_a -= stop
                tokens_b -= stop
                if not tokens_a or not tokens_b:
                    return 0.0
                shorter = tokens_a if len(tokens_a) <= len(tokens_b) else tokens_b
                longer = tokens_b if len(tokens_a) <= len(tokens_b) else tokens_a
                overlap = shorter & longer
                return len(overlap) / max(len(shorter), 1)

            # Build combined PO text: material_code + description + any spec info
            po_text = f"{mat} {material_description_cleaned}"

            best_desc_idx = None
            best_desc_score = 0.0
            for idx, row in cust_df.iterrows():
                cmat_val = str(row[self.col_cust_mat]).strip()
                if not _is_desc_fragment(cmat_val):
                    continue  # only attempt on description-like CMIR values
                score = _word_overlap(po_text, cmat_val)
                if score > best_desc_score:
                    best_desc_score = score
                    best_desc_idx = idx

            if best_desc_score >= 0.40 and best_desc_idx is not None:
                matches = cust_df.loc[[best_desc_idx]]
                print(
                    f"    [Mapper] CMIR-desc match: PO desc '{mat} {material_description_cleaned[:40]}' "
                    f"matched CMIR fragment '{str(cust_df.at[best_desc_idx, self.col_cust_mat])[:60]}' "
                    f"(overlap={best_desc_score:.2f})"
                )


        # When this customer does not have a mapping for the extracted code
        # (e.g. ABB 4020028890 ordering K225-KS which is only registered under
        # ABB France 4020002046), try to find the internal SAP material number
        # by matching the extracted code against material_description in the full
        # Test MP. The customer_material_number is left blank to flag missing
        # master data — the CSR / data team must add it in Celonis.
        if matches.empty and self.col_mat_desc and mat and len(cust) >= 5:
            def clean_code(s: str) -> str:
                return "".join(c for c in str(s) if c.isalnum()).lower()
            clean_mat = clean_code(mat)
            if len(clean_mat) >= 4:  # avoid matching very short codes globally
                desc_series = self.df[self.col_mat_desc].dropna().astype(str).str.strip()
                clean_descs = desc_series.str.replace(r'[^a-zA-Z0-9]', '', regex=True).str.lower()
                
                # Check where clean_mat is in clean_ref_desc
                match_mask = clean_descs.str.contains(clean_mat, regex=False)
                
                # Check the reverse: clean_ref_desc in clean_mat
                short_descs = clean_descs[clean_descs.str.len() <= len(clean_mat)]
                reverse_matches = short_descs[short_descs.apply(lambda x: len(x) >= 4 and x in clean_mat)]
                
                all_match_indices = match_mask[match_mask].index.union(reverse_matches.index)
                
                if not all_match_indices.empty:
                    matched_idx = all_match_indices[0]
                    row = self.df.loc[matched_idx]
                    internal = str(row.get(self.col_material, "")).strip() if self.col_material else ""
                    if internal:
                        safe_mat = mat.encode('ascii', 'replace').decode('ascii')
                        safe_cust = cust.encode('ascii', 'replace').decode('ascii')
                        print(
                            f"    [Mapper] GLOBAL fallback: '{safe_mat}' not in "
                            f"customer {safe_cust} master data — found internal "
                            f"'{internal}' via global description match "
                            f"(add {safe_mat} to Celonis for this customer)."
                        )
                        return {
                            "internal_material_number": internal,
                            "sales_organization": str(row.get(self.col_sales_org, "")).strip()
                                if self.col_sales_org else None,
                            "order_type": str(row.get(self.col_order_type, "")).strip()
                                if self.col_order_type else None,
                            "customer_material_number": "",  # intentionally blank — not in master data
                            "so_split_rule": "X",
                            "is_global_fallback": True,
                        }

        # ── Global Description Fallback: search ALL materials by grade tokens ──
        # When mat code is customer-internal (e.g. Nagase '100876583') and not in CMIR,
        # match grade tokens from description (e.g. 'B4215 901510') against full master data.
        if matches.empty and self.col_mat_desc and material_description_cleaned and len(cust) >= 5:
            ext_tokens = _get_grade_tokens(material_description_cleaned)
            if ext_tokens and len(ext_tokens) >= 2:
                for idx, row in self.df.iterrows():
                    ref_desc = str(row.get(self.col_mat_desc, "")).strip()
                    if not ref_desc:
                        continue
                    ref_tokens = set(re.sub(r'[^a-z0-9]', ' ', ref_desc.lower()).split())
                    _ov = ext_tokens & ref_tokens
                    _fwd = len(_ov) / len(ext_tokens)
                    _rev = len(_ov) / len(ref_tokens) if ref_tokens else 0
                    # Global fallback: stricter reverse threshold (0.70) to avoid false positives
                    if ext_tokens.issubset(ref_tokens) or _fwd >= 0.75 or (len(ref_tokens) >= 3 and _rev >= 0.70):
                        internal = str(row.get(self.col_material, "")).strip() if self.col_material else ""
                        if internal:
                            safe_mat = mat.encode('ascii', 'replace').decode('ascii')
                            safe_cust = cust.encode('ascii', 'replace').decode('ascii')
                            print(
                                f"    [Mapper] GLOBAL description fallback: '{material_description_cleaned[:40]}' "
                                f"matched internal '{internal}' ({ref_desc}) globally."
                            )
                            return {
                                "internal_material_number": internal,
                                "sales_organization": str(row.get(self.col_sales_org, "")).strip()
                                    if self.col_sales_org else None,
                                "order_type": str(row.get(self.col_order_type, "")).strip()
                                    if self.col_order_type else None,
                                "customer_material_number": "",
                                "so_split_rule": "X",
                                "is_global_fallback": True,
                            }


        # ── Old-name / New-name slash-split retry ─────────────────────────────
        # Some customers (notably Pegasus Polymers) write BOTH the old Akulon name
        # AND the new Durethan/Stanyl name in one description string, separated by '/':
        #   e.g. "Akulon K222-KGV4/BK25019/ Durethan BKV20FN21 90011 BLACK BK25019"
        # We try each slash-separated segment (LAST first = newest Envalior brand) to
        # get a CMIR hit.  The full lookup() is called recursively for each segment.
        if matches.empty and '/' in mat:
            segments = [s.strip() for s in mat.split('/') if s.strip()]
            for seg in reversed(segments):   # try LAST segment first (new name)
                if seg == mat or len(seg) < 3:
                    continue
                seg_result = self.lookup(cust, seg, material_description, packaging)
                if seg_result:
                    print(
                        f"    [Mapper] Slash-split match: '{mat[:40]}' -> "
                        f"segment '{seg}' matched CMIR (old/new name rebranding)"
                    )
                    return seg_result

        if matches.empty:
            return None


        # Smart tie-breaker when multiple rows match (e.g. Oktabin vs Silo)
        if len(matches) > 1:
            best_row = None
            best_score = -100

            pkg_lower = str(packaging).lower().strip()
            desc_lower = str(material_description).lower().strip()

            # Common packaging keywords we look for
            pkg_keywords = ["silo", "oktabin", "octabin", "bulk", "bag", "pallet", "drum"]
            po_pkg_keywords = [kw for kw in pkg_keywords if kw in pkg_lower or kw in desc_lower]

            # Normalize octabin/oktabin
            if "octabin" in po_pkg_keywords and "oktabin" not in po_pkg_keywords:
                po_pkg_keywords.append("oktabin")
            if "oktabin" in po_pkg_keywords and "octabin" not in po_pkg_keywords:
                po_pkg_keywords.append("octabin")

            for _, r in matches.iterrows():
                score = 0
                db_cust_mat = str(r.get(self.col_cust_mat, "") or "").lower().strip()
                db_mat_desc = str(r.get(self.col_mat_desc, "") or "").lower().strip()

                # Score based on packaging keywords
                has_pkg_match = False
                for kw in po_pkg_keywords:
                    if kw in db_cust_mat or kw in db_mat_desc:
                        has_pkg_match = True
                        score += 10

                # Explicit penalties for conflicting packaging types
                if "silo" in po_pkg_keywords and any(kw in db_cust_mat or kw in db_mat_desc for kw in ["oktabin", "octabin"]):
                    score -= 15
                if any(kw in po_pkg_keywords for kw in ["oktabin", "octabin"]) and "silo" in db_cust_mat:
                    score -= 15

                # Description similarity matching
                if desc_lower and db_mat_desc:
                    def clean_text(s: str) -> str:
                        return "".join(c for c in s if c.isalnum()).lower()
                    clean_po = clean_text(desc_lower)
                    clean_db = clean_text(db_mat_desc)

                    if clean_po in clean_db or clean_db in clean_po:
                        score += 5
                    
                    from difflib import SequenceMatcher
                    ratio = SequenceMatcher(None, clean_po, clean_db).ratio()
                    score += ratio * 5

                if score > best_score:
                    best_score = score
                    best_row = r

            if best_row is not None:
                row = best_row
            else:
                row = matches.iloc[0]
        else:
            row = matches.iloc[0]

        result = {
            "internal_material_number": str(row.get(self.col_material, "")).strip() if self.col_material else None,
            "sales_organization": str(row.get(self.col_sales_org, "")).strip() if self.col_sales_org else None,
            "order_type": str(row.get(self.col_order_type, "")).strip() if self.col_order_type else None,
            "customer_material_number": str(row.get(self.col_cust_mat, "")).strip() if self.col_cust_mat else None,
            "so_split_rule": str(row.get(self.col_so_split, "X")).strip().upper() if self.col_so_split else "X",
        }
        return result

    def lookup_by_customer_only(self, customer_number: str) -> Optional[Dict]:
        """
        Get default mapping for a customer (first matching row).
        Useful when material code is not matched.
        """
        if not customer_number:
            return None
        cust = str(customer_number).strip()
        mask = self.df[self.col_customer] == cust
        matches = self.df[mask]
        if matches.empty:
            return None
        row = matches.iloc[0]
        return {
            "sales_organization": str(row.get(self.col_sales_org, "")).strip() if self.col_sales_org else None,
            "order_type": str(row.get(self.col_order_type, "")).strip() if self.col_order_type else None,
            "so_split_rule": str(row.get(self.col_so_split, "X")).strip().upper() if self.col_so_split else "X",
        }

    def _get_customer_group2(self, customer_id: str) -> str:
        """
        Look up customer_group2 from Test MP raw data for SO-split logic.

        Business rules:
            'Z01'  → Separate Sales Order per line item
            '-'    → One Sales Order per PO  (all line items together)
            ''     → Not found / Excel mode (fall back to Excel so_split_rule column)
        """
        # Support both _df_mp_raw (new) and _df2_raw (backward compat)
        raw = getattr(self, '_df_mp_raw', None)
        if raw is None:
            raw = getattr(self, '_df2_raw', None)
        if raw is None or raw.empty:
            return ''
        if 'customer_group2' not in raw.columns:
            return ''

        # Test MP uses 'sold_to_id'; old Sheet 2 used 'customer_id'
        id_col = 'sold_to_id' if 'sold_to_id' in raw.columns else 'customer_id'
        if id_col not in raw.columns:
            return ''

        cid  = str(customer_id).strip()
        mask = raw[id_col].astype(str).str.strip() == cid
        rows = raw[mask]
        if rows.empty:
            return ''

        vals = set(rows['customer_group2'].fillna('').astype(str).str.strip())
        if 'Z01' in vals:
            return 'Z01'
        non_empty = [v for v in vals if v not in ('nan', 'None', '', '-')]
        if non_empty:
            return non_empty[0]
        return '-'

    def get_customer_location(self, customer_id: str) -> dict:
        """
        Return city and postcode for a given customer/sold-to ID from Test MP.
        Used for address-based disambiguation when multiple customers
        have similar names (e.g. Molex LLC USA vs Nihon Molex Japan).

        Returns:
            dict with keys:
              'cities'    – set of city strings (lowercase, combined SoldToCity1+2)
              'postcodes' – set of postcode strings (from combined SoldToPostCode1-3)
        """
        result = {'cities': set(), 'postcodes': set()}
        raw = getattr(self, '_df_mp_raw', None)
        if raw is None:
            raw = getattr(self, '_df2_raw', None)
        if raw is None or raw.empty:
            return result

        # Test MP uses 'sold_to_id'; old Sheet 2 used 'customer_id'
        id_col = 'sold_to_id' if 'sold_to_id' in raw.columns else 'customer_id'
        if id_col not in raw.columns:
            return result

        cid  = str(customer_id).strip()
        mask = raw[id_col].astype(str).str.strip() == cid
        rows = raw[mask]
        if rows.empty:
            return result

        if 'sold_to_city' in rows.columns:
            result['cities'] = set(
                v.lower().strip() for v in rows['sold_to_city'].fillna('').astype(str).unique()
                if v.strip() and v.strip().lower() not in ('', 'nan', 'none')
            )
        elif 'city' in rows.columns:  # backward compat with old Sheet 2
            result['cities'] = set(
                v.lower().strip() for v in rows['city'].fillna('').astype(str).unique()
                if v.strip() and v.strip().lower() not in ('', 'nan', 'none')
            )

        if 'sold_to_postcode' in rows.columns:
            result['postcodes'] = set(
                v.lower().strip() for v in rows['sold_to_postcode'].fillna('').astype(str).unique()
                if v.strip() and v.strip().lower() not in ('', 'nan', 'none')
            )

        return result

    def get_salesperson_info(self, customer_id: str, sales_org: str = "") -> dict:
        """
        Look up Employee Responsible (salesperson_name) and Email Responsible (salesperson_email)
        from Test MP raw data for a given customer/sold-to ID.

        If master data email is blank, falls back to regional CSR email from csr_routing.py.
        """
        res = {"employee_responsible": "", "email_responsible": ""}
        raw = getattr(self, '_df_mp_raw', None)
        if raw is None:
            raw = getattr(self, '_df2_raw', None)

        if raw is not None and not raw.empty:
            id_col = 'sold_to_id' if 'sold_to_id' in raw.columns else 'customer_id'
            if id_col in raw.columns:
                cid = str(customer_id).strip()
                mask = raw[id_col].astype(str).str.strip() == cid
                rows = raw[mask]
                if not rows.empty:
                    if 'salesperson_name' in rows.columns:
                        names = [v.strip() for v in rows['salesperson_name'].fillna('').unique()
                                 if v.strip() and v.strip().lower() not in ('', 'nan', 'none')]
                        if names:
                            res["employee_responsible"] = names[0]

                    if 'salesperson_email' in rows.columns:
                        emails = [v.strip() for v in rows['salesperson_email'].fillna('').unique()
                                  if v.strip() and v.strip().lower() not in ('', 'nan', 'none')]
                        if emails:
                            res["email_responsible"] = emails[0]

        # Fallback for email_responsible: resolve via 3-Tier CSR Routing.
        # IMPORTANT: For customers active in multiple sales orgs (e.g. GEWISS: 2545/2500/2540),
        # the item's sales_org may be empty when the material is unmapped.
        # Try the provided sales_org first; if that fails, try all orgs from TestMP for this customer.
        if not res["email_responsible"]:
            try:
                from csr_routing import resolve_csr_routing

                # Build list of sales orgs to try: provided one first, then all from TestMP
                orgs_to_try = []
                if sales_org:
                    orgs_to_try.append(sales_org)
                # Also collect all orgs from TestMP for this customer
                if raw is not None and not raw.empty:
                    id_col = 'sold_to_id' if 'sold_to_id' in raw.columns else 'customer_id'
                    if id_col in raw.columns:
                        cid = str(customer_id).strip()
                        cust_rows = raw[raw[id_col].astype(str).str.strip() == cid]
                        if not cust_rows.empty and 'sales_organization' in cust_rows.columns:
                            for org in cust_rows['sales_organization'].dropna().unique():
                                org_str = str(org).strip()
                                if org_str and org_str not in orgs_to_try:
                                    orgs_to_try.append(org_str)

                for try_org in orgs_to_try:
                    to_recip, _, _, _ = resolve_csr_routing(
                        sales_org=try_org,
                        customer_number=customer_id,
                    )
                    if to_recip:
                        res["email_responsible"] = to_recip[0]
                        if try_org != sales_org:
                            print(f"  [CSR] email_responsible resolved via fallback org {try_org!r}: {to_recip[0]}")
                        break

                # Last resort: try without org constraint
                if not res["email_responsible"]:
                    to_recip, _, _, _ = resolve_csr_routing(
                        sales_org="",
                        customer_number=customer_id,
                    )
                    if to_recip:
                        res["email_responsible"] = to_recip[0]
                        print(f"  [CSR] email_responsible resolved via no-org fallback: {to_recip[0]}")

            except Exception as _csr_err:
                print(f"  [CSR] Warning: CSR routing lookup failed: {_csr_err}")

        return res


    def get_ship_to_info_from_test_mp(
        self,
        customer_id: str,
        ship_to_id: str,
        ship_to_address: str = '',
        ship_to_postcode: str = '',
        ship_to_name: str = '',
        ship_to_city: str = '',
    ) -> dict:
        """
        Validate and correct the ship-to ID and name using Test MP data.

        Priority:
          0. Chinese Plant / Warehouse Designation / Short Name matching
             (e.g. '訊知昂宏倉' -> '勗宏倉儲' 4020033101, '淵興' -> '淵興公司' 4020033896,
              '耐特' -> '耐特科技材料...' 4020023959)
          1. If extracted ship_to_id exists in Test MP for this customer → keep it.
          1.5. If ship_to_postcode provided → match against ship_to_postcode.
          2. Fuzzy match combined address / name against Test MP ship_to_name_full.
          3. Default to Sold-To ID ONLY if no delivery info (name/address/city) provided.

        Returns:
            dict with keys 'ship_to_id' and 'ship_to_name'.
        """
        result = {'ship_to_id': ship_to_id, 'ship_to_name': ''}
        raw = getattr(self, '_df_mp_raw', None)
        if raw is None or raw.empty:
            return result

        id_col = 'sold_to_id' if 'sold_to_id' in raw.columns else 'customer_id'
        if id_col not in raw.columns or 'ship_to_id' not in raw.columns:
            return result

        # Get all ship-to entries for this customer from Test MP
        cust_rows = raw[raw[id_col].astype(str).str.strip() == str(customer_id).strip()]
        if cust_rows.empty:
            return result

        # Deduplicate on ship_to_id for lookup efficiency
        ship_rows = cust_rows.drop_duplicates(subset=['ship_to_id'])

        # ── Priority 1: Exact match on explicit ship_to_id ────────────────────
        # If an explicit ship_to_id was extracted (or provided in email body) and exists for this customer, keep it.
        if ship_to_id:
            match = ship_rows[ship_rows['ship_to_id'].astype(str).str.strip() == str(ship_to_id).strip()]
            if not match.empty:
                name = ''
                if 'ship_to_name_full' in match.columns:
                    name = str(match['ship_to_name_full'].iloc[0] or '').strip()
                result['ship_to_id']   = str(ship_to_id).strip()
                result['ship_to_name'] = name
                return result

        # ── Priority 0: Chinese Plant / Warehouse Designation & Short Name Matching ──
        _TRAD_TO_SIMP = str.maketrans({
            '廠': '厂', '倉': '仓', '貨': '货', '發': '发', '東': '东',
            '灣': '湾', '臺': '台', '國': '国', '華': '华', '寶': '宝',
            '興': '兴', '實': '实', '業': '业', '體': '体', '組': '组',
            '設': '设', '備': '备', '統': '统', '產': '产', '廣': '广',
            '（': '(', '）': ')', '：': ':', '；': ';', '，': ',',
            '勗': '昂',  # character variant alias (勗 <-> 昂 in Taiwanese warehouse names)
        })

        combined_search = f"{ship_to_name} {ship_to_address} {ship_to_city}".strip()
        addr_norm = combined_search.translate(_TRAD_TO_SIMP).upper() if combined_search else ''

        # Check if PO address has a plant/warehouse letter (e.g. 'K', 'B', 'F', 'G', 'H')
        plant_letter = None
        if addr_norm:
            m_plant = re.search(r'(?:DELIVERY TO\s*:?|货仓|仓|厂|WAREHOUSE|PLANT)\s*([A-Z0-9]{1,3})', addr_norm)
            if m_plant:
                plant_letter = m_plant.group(1)
            else:
                m_plant2 = re.search(r'([A-Z0-9]{1,3})\s*(?:厂|仓|库|房|PLANT|WAREHOUSE)?\s*$', addr_norm)
                if m_plant2:
                    plant_letter = m_plant2.group(1)

        if plant_letter:
            target_pattern = f"{plant_letter}厂"
            for _, r in ship_rows.iterrows():
                st_name = str(r.get('ship_to_name_full', '') or '').translate(_TRAD_TO_SIMP).upper()
                st_id   = str(r['ship_to_id']).strip()
                if target_pattern in st_name or f" {plant_letter}" in st_name or st_name.endswith(plant_letter):
                    print(f"  [ShipTo-PLANT] Plant/Warehouse designation '{plant_letter}' matched ship-to {st_id} ({st_name[:40]})")
                    result['ship_to_id']   = st_id
                    result['ship_to_name'] = str(r.get('ship_to_name_full', '') or '').strip()
                    return result

        # ── Priority 0.5: Chinese Short Warehouse/Plant Name Search ───────────
        if combined_search and len(ship_rows) > 1:
            clean_search = ship_to_name.strip() or combined_search
            # Remove sold-to customer name prefixes if embedded (e.g. '訊知' from '訊知昂宏倉')
            full_cust_name = str(cust_rows.iloc[0].get('sold_to_name_full', '') or '').strip()
            cust_prefix = re.sub(r'(?:股份有限公司|有限公司|股份公司|公司|Corporation|Inc|Ltd).*', '', full_cust_name).strip()
            if cust_prefix and len(cust_prefix) >= 2:
                clean_search = clean_search.replace(cust_prefix, '').strip()

            search_norm = clean_search.translate(_TRAD_TO_SIMP)
            c_matches = []
            for _, r in ship_rows.iterrows():
                st_id = str(r['ship_to_id']).strip()
                st_name = str(r.get('ship_to_name_full', '') or '').strip()
                st_name_norm = st_name.translate(_TRAD_TO_SIMP)

                score = 0
                # Direct containment match (e.g. '淵興' in '淵興公司', '耐特' in '耐特科技材料...')
                if clean_search and (clean_search in st_name or search_norm in st_name_norm):
                    score += 10
                if ship_to_name and (ship_to_name in st_name or ship_to_name.translate(_TRAD_TO_SIMP) in st_name_norm):
                    score += 10

                # Character overlap matching for short names (e.g. '昂宏倉' matching '勗宏倉儲')
                if not score and len(search_norm) >= 2:
                    core_chars = [c for c in search_norm if c not in ('仓', '厂', '库', '店', '館', '館')]
                    if core_chars:
                        matched_chars = [c for c in core_chars if c in st_name_norm]
                        if len(matched_chars) >= len(core_chars):
                            score += 8

                if score > 0:
                    c_matches.append((score, st_id, st_name))

        # ── Priority 0.8: Incoterm & Delivery Destination City Matching ───────
        # When a PO specifies an Incoterm delivery destination (e.g. 'CIP Freilassing', 'DAP <City>',
        # 'FCA <City>') or delivery location, the physical freight handover destination city takes
        # precedence over the general billing/legal plant address.
        if combined_search and len(ship_rows) > 1 and 'ship_to_city' in ship_rows.columns:
            m_incoterm = re.search(r'\b(?:CIP|DAP|DDP|FCA|CPT|FOB|CIF|Lieferort|Delivery\s*to)\s*:?\s*([A-Z\u00c0-\u0178a-z\u00e0-\u00ff]{3,25})\b', combined_search, re.IGNORECASE)
            if m_incoterm:
                inco_city = m_incoterm.group(1).strip()
                inco_matches = ship_rows[ship_rows['ship_to_city'].astype(str).str.lower().apply(
                    lambda x: inco_city.lower() in x.split() or inco_city.lower() in x
                )]
                if not inco_matches.empty:
                    # If multiple ship-tos exist in this destination city, prefer the transport/logistics partner or closest name match
                    best_inco_row = inco_matches.iloc[0]
                    for _, ir in inco_matches.iterrows():
                        ir_name = str(ir.get('ship_to_name_full', '') or '')
                        if 'Transport' in ir_name:
                            best_inco_row = ir
                            break
                    st_id = str(best_inco_row['ship_to_id']).strip()
                    st_name = str(best_inco_row.get('ship_to_name_full', '') or '').strip()
                    print(f"  [ShipTo-INCOTERM] Delivery term '{m_incoterm.group(0)}' matched destination city '{inco_city}' -> ship-to {st_id} ({st_name[:40]})")
                    result['ship_to_id'] = st_id
                    result['ship_to_name'] = st_name
                    return result



        # ── Priority 1: Postcode Matching ─────────────────────────────────────

        # NOTE: Postcode check runs BEFORE the blank-address fallback, because a PO may
        # provide a postcode without a full address text (e.g. GEWISS-style POs).
        if ship_to_postcode and 'ship_to_postcode' in ship_rows.columns:
            pc_clean = str(ship_to_postcode).strip().replace('-', '').replace(' ', '')
            if pc_clean:
                pc_mask = ship_rows['ship_to_postcode'].astype(str).apply(
                    lambda x: (lambda tokens: any(
                        pc_clean == t.replace('-', '').replace(' ', '')
                        for t in (
                            tokens
                            + [''.join(tokens[i:i+2]).replace('-','').replace(' ','') for i in range(len(tokens)-1)]
                        )
                    ))(x.split())
                )
                pc_hits = ship_rows[pc_mask]
                if len(pc_hits) == 1:
                    best_row = pc_hits.iloc[0]
                    pc_name = str(best_row.get('ship_to_name_full', '') or '').strip()
                    pc_id   = str(best_row['ship_to_id']).strip()
                    print(f"  [ShipTo] Postcode {pc_clean!r} matched customer ship-to: {pc_id} ({pc_name})")
                    result['ship_to_id']   = pc_id
                    result['ship_to_name'] = pc_name
                    return result
                elif len(pc_hits) > 1:
                    addr_lower = ship_to_address.lower().strip()

                    import re as _re

                    # ── STEP 0: Exact postcode-only rows get a strong head start ──────────
                    # When TestMP has rows with different postcodes (e.g. 24050 and 24069)
                    # but our pc_clean is '24050', only the row whose postcode IS '24050'
                    # should be considered.  Re-filter to exact matches first.
                    def _exact_pc(raw_pc: str) -> bool:
                        """True if raw_pc contains pc_clean as a standalone token (exact match)."""
                        for token in str(raw_pc).split():
                            if token.replace('-', '').replace(' ', '') == pc_clean:
                                return True
                        return False

                    exact_hits = pc_hits[
                        pc_hits['ship_to_postcode'].astype(str).apply(_exact_pc)
                    ]
                    # If exactly one row has an exact postcode match → return it immediately
                    if len(exact_hits) == 1:
                        best_row = exact_hits.iloc[0]
                        pc_name = str(best_row.get('ship_to_name_full', '') or '').strip()
                        pc_id   = str(best_row['ship_to_id']).strip()
                        print(f"  [ShipTo] Postcode {pc_clean!r} exact-matched to unique ship-to: {pc_id} ({pc_name})")
                        result['ship_to_id']   = pc_id
                        result['ship_to_name'] = pc_name
                        return result

                    # Use exact_hits for scoring if we got some; otherwise fall back to all hits
                    score_candidates = exact_hits if not exact_hits.empty else pc_hits

                    # ── STEP 1: Try to extract city from PO address text ───────────────────
                    # E.g. "Via L. Galvani 1, 24050 CALCINATE (BG), ITALY" → city = "CALCINATE"
                    _city_from_addr = ship_to_city  # use explicit field if already available
                    if not _city_from_addr and ship_to_address:
                        # Look for city after a postcode in the address string
                        city_m = _re.search(
                            r'\b' + _re.escape(pc_clean) + r'\b[\s,]+([A-ZÀ-Ÿa-zà-ÿ][A-ZÀ-Ÿa-zà-ÿ\s-]{1,30}?)(?:\s*[\(,]|$)',
                            ship_to_address, _re.IGNORECASE
                        )
                        if city_m:
                            _city_from_addr = city_m.group(1).strip()
                            print(f"  [ShipTo] Auto-extracted city from address: '{_city_from_addr}'")

                    best_score = -1.0
                    best_row = score_candidates.iloc[0]

                    specific_tokens = set()
                    for m in _re.finditer(r'\bGeb(?:\u00e4ude)?\.?\s*([A-Z]\s*\d+|\d+)\b', ship_to_address, _re.IGNORECASE):
                        specific_tokens.add(_re.sub(r'\s+', '', m.group(1).upper()))
                    for m in _re.finditer(r'\b([A-Z]{1,3}\s*\d{2,4})\b', ship_to_address):
                        specific_tokens.add(_re.sub(r'\s+', '', m.group(1).upper()))
                    for m in _re.finditer(r'\b([A-Z]{3,6})\b', ship_to_address):
                        tok = m.group(1)
                        if tok not in ('DAP', 'FOB', 'CIF', 'EUR', 'DIN', 'ISO', 'VAT', 'NRW', 'PLZ', 'GEB', 'STR', 'AG', 'SPA', 'VIA', 'AND', 'THE'):
                            specific_tokens.add(tok)

                    for _, row in score_candidates.iterrows():
                        name = str(row.get('ship_to_name_full') or '').strip()
                        city = str(row.get('ship_to_city') or '').strip()
                        score = 0.0

                        # Generic word overlap (company name words in PO address)
                        if name and addr_lower:
                            words = name.lower().split()
                            overlap = sum(1 for w in words if len(w) > 3 and w in addr_lower)
                            score += (overlap / max(len(words), 1)) * 2.0

                        # ── City match bonus (HIGH priority) ─────────────────────────
                        # Match DB city against PO address text
                        if city and addr_lower and city.lower() in addr_lower:
                            score += 6.0  # raised from 3.0 — city match is very reliable
                            print(f"    [ShipTo] City name '{city}' found in PO address for ship-to {row['ship_to_id']}")

                        # Match DB city against extracted city token from address
                        if city and _city_from_addr:
                            if city.lower().strip() == _city_from_addr.lower().strip():
                                score += 8.0  # exact city token match — very high confidence
                                print(f"    [ShipTo] City exact match: PO city='{_city_from_addr}' == DB city='{city}' for ship-to {row['ship_to_id']}")
                            elif city.lower().strip() in _city_from_addr.lower() or _city_from_addr.lower() in city.lower().strip():
                                score += 4.0

                        # Match supplied ship_to_city field directly
                        if city and ship_to_city and city.lower().strip() == ship_to_city.lower().strip():
                            score += 8.0
                            print(f"    [ShipTo] City field exact match: '{ship_to_city}' == '{city}' for ship-to {row['ship_to_id']}")

                        # Sequence match
                        from difflib import SequenceMatcher
                        if addr_lower:
                            seq_score = SequenceMatcher(None, name.lower(), addr_lower).ratio()
                            score += seq_score * 1.0

                        # ── SPECIFIC IDENTIFIER BOOST ────────────────────────────
                        if specific_tokens and name:
                            name_upper = _re.sub(r'\s+', '', name.upper())
                            for tok in specific_tokens:
                                if len(tok) >= 2 and tok in name_upper:
                                    score += 10.0
                                    print(f"    [ShipTo] Specific identifier {tok!r} matched in ship-to {row['ship_to_id']} ({name[:50]})")

                        if score > best_score:
                            best_score = score
                            best_row = row

                    pc_name = str(best_row.get('ship_to_name_full', '') or '').strip()
                    pc_id   = str(best_row['ship_to_id']).strip()
                    print(f"  [ShipTo] Postcode {pc_clean!r} matched multiple customer ship-tos. Disambiguated to {pc_id} ({pc_name}) with score {best_score:.2f}")
                    result['ship_to_id']   = pc_id
                    result['ship_to_name'] = pc_name
                    return result
 
        # -- Priority 1b: global postcode match fallback (search ALL ship-tos, not just this customer) --
        if ship_to_postcode and 'ship_to_postcode' in raw.columns:
            pc_clean = str(ship_to_postcode).strip().replace('-', '').replace(' ', '')
            if pc_clean:
                # Search across ALL rows (not just this customer)
                pc_mask = raw['ship_to_postcode'].astype(str).apply(
                    lambda x: any(pc_clean == token.replace('-', '').replace(' ', '') for token in x.split())
                )
                pc_hits = raw[pc_mask].drop_duplicates(subset=['ship_to_id'])
                if not pc_hits.empty:
                    best_row = pc_hits.iloc[0]
                    pc_name = str(best_row.get('ship_to_name_full', '') or '').strip()
                    pc_id   = str(best_row['ship_to_id']).strip()
                    result['ship_to_id']   = pc_id
                    result['ship_to_name'] = pc_name
                    return result

        # -- Priority 1: exact match on ship_to_id --
        if ship_to_id:
            match = ship_rows[ship_rows['ship_to_id'].astype(str).str.strip() == str(ship_to_id).strip()]
            if not match.empty:
                name = ''
                if 'ship_to_name_full' in match.columns:
                    name = str(match['ship_to_name_full'].iloc[0] or '').strip()
                result['ship_to_name'] = name
                return result

        # -- Priority 1.5: Chinese city romanization matching --
        # When the PO address contains romanized Chinese city names (e.g. 'Dongguan', 'Guangdong'),
        # match them against the ship_to_city column (which may contain Chinese characters).
        # This handles the case where: PO says "Dongguan City, Guangdong Province"
        # and Test MP has city "广东省东莞市" — the HONGKONG ship-to should be excluded.
        if ship_to_address and 'ship_to_city' in ship_rows.columns:
            addr_lower = ship_to_address.lower().strip()
            # Romanization map: roman keyword → list of Chinese city equivalents to match
            _CITY_ROMANIZATION = {
                'dongguan':  ['东莞', '广东省东莞市'],
                'shenzhen':  ['深圳', '广东省深圳市'],
                'guangzhou': ['广州', '广东省广州市'],
                'guangdong': ['广东'],
                'jiangsu':   ['江苏'],
                'nanjing':   ['南京', '江苏省南京市'],
                'wuxi':      ['无锡', '江苏省无锡市'],
                'shanghai':  ['上海'],
                'beijing':   ['北京'],
                'tianjin':   ['天津'],
                'wuhan':     ['武汉', '湖北省武汉市'],
                'chengdu':   ['成都', '四川省成都市'],
                'qishi':     ['祁石', '东莞'],
                'hangzhou':  ['杭州', '浙江省杭州市'],
            }
            # Build the set of Chinese city strings to look for, from addr
            target_cities = set()
            for roman, chinese_list in _CITY_ROMANIZATION.items():
                if roman in addr_lower:
                    target_cities.update(chinese_list)
            # Also check for HONGKONG exclusion: if addr says HK, don't use mainland
            _addr_is_hk_15 = any(sig in addr_lower for sig in ('hong kong', 'hongkong', 'hksar'))
            if _addr_is_hk_15:
                target_cities.discard('东莞')  # don't accidentally exclude HK→mainland flip
            if target_cities:
                city_match_rows = ship_rows[
                    ship_rows['ship_to_city'].astype(str).apply(
                        lambda c: any(tc in c for tc in target_cities)
                    )
                ]
                # Exclude HONGKONG from candidates when PO delivery is mainland
                if not _addr_is_hk_15:
                    city_match_rows = city_match_rows[
                        ~city_match_rows['ship_to_city'].astype(str).str.upper().str.contains('HONG', na=False)
                    ]
                if len(city_match_rows) >= 1:
                    if len(city_match_rows) == 1:
                        best_row = city_match_rows.iloc[0]
                    else:
                        # Multiple mainland ship-tos matched. Pick the most specific:
                        # Score each by the length of the longest target_city found in its city string.
                        # Longer match = more specific city = better candidate.
                        best_row = city_match_rows.iloc[0]
                        best_city_score = 0
                        for _, cr in city_match_rows.iterrows():
                            city_val = str(cr.get('ship_to_city', '') or '')
                            city_score = max((len(tc) for tc in target_cities if tc in city_val), default=0)
                            if city_score > best_city_score:
                                best_city_score = city_score
                                best_row = cr
                    c_id   = str(best_row['ship_to_id']).strip()
                    c_name = str(best_row.get('ship_to_name_full', '') or '').strip()
                    try:
                        print(f"  [ShipTo-CITY] Romanization match: '{addr_lower[:40]}...' -> {c_id} ({c_name[:30]})")
                    except UnicodeEncodeError:
                        print(f"  [ShipTo-CITY] Romanization match: '{addr_lower[:40]}...' -> {c_id}")
                    result['ship_to_id']   = c_id
                    result['ship_to_name'] = c_name
                    return result


        # -- Priority 2: fuzzy address match against Test MP ship-to names --
        if ship_to_address and 'ship_to_name_full' in ship_rows.columns:
            addr_lower = ship_to_address.lower().strip()

            # Pre-determine delivery geography from the PO address:
            # We use this to heavily penalize ship-to entries from the wrong country.
            _MAINLAND_SIGNALS  = ('guangdong', 'dongguan', 'shenzhen', 'guangzhou', 'china',
                                   'jiangsu', 'shanghai', 'beijing', 'wuhan', 'nanjing',
                                   'hangzhou', 'chengdu', 'qishi', 'foshan', 'zhuhai',
                                   'guangdong province', 'cn', 'p.r.c', 'p.r. china')
            _HK_SIGNALS        = ('hong kong', 'hongkong', 'hksar', 'kowloon', 'hong-kong')
            _addr_is_mainland  = any(sig in addr_lower for sig in _MAINLAND_SIGNALS)
            _addr_is_hk        = any(sig in addr_lower for sig in _HK_SIGNALS)

            best_score = 0.0
            best_id   = ship_to_id
            best_name = ''
            for _, row in ship_rows.iterrows():
                name = str(row.get('ship_to_name_full') or '').strip()
                if not name:
                    continue
                # Partial word overlap score: how many words of the name appear in the address
                words     = name.lower().split()
                overlap   = sum(1 for w in words if len(w) > 3 and w in addr_lower)
                score     = overlap / max(len(words), 1)
                # Supplement with SequenceMatcher
                from difflib import SequenceMatcher
                seq_score = SequenceMatcher(None, name.lower(), addr_lower).ratio()

                # City name match bonus
                city = str(row.get('ship_to_city') or '').strip()
                city_bonus = 0.0
                if city and city.lower() in addr_lower:
                    city_bonus = 0.5

                # ── Geographic exclusion penalty ─────────────────────────────
                # If PO delivery address is mainland China, heavily penalize HK ship-tos.
                # If PO delivery address is HK, heavily penalize mainland China ship-tos.
                # This prevents e.g. "Dongguan Tetro" from mapping to HONGKONG ship-to.
                city_upper = city.upper()
                geo_penalty = 0.0
                if _addr_is_mainland and not _addr_is_hk:
                    if 'HONG' in city_upper or 'HK' == city_upper or 'HONGKONG' in city_upper.replace(' ',''):
                        geo_penalty = -5.0  # decisive: cannot win if delivery is mainland
                elif _addr_is_hk and not _addr_is_mainland:
                    # Mainland city signals (Chinese characters or known mainland cities)
                    mainland_city_sigs = ('guangdong', 'dongguan', 'shenzhen', 'guangzhou',
                                          'jiangsu', 'shanghai', 'beijing', 'nanjing', '东莞',
                                          '广东', '江苏', '上海', '北京')
                    if any(sig in city.lower() for sig in mainland_city_sigs):
                        geo_penalty = -5.0  # decisive: cannot win if delivery is HK

                combined  = 0.6 * score + 0.4 * seq_score + city_bonus + geo_penalty
                if combined > best_score:
                    best_score = combined
                    best_id    = str(row['ship_to_id']).strip()
                    best_name  = name
                    if geo_penalty < 0:
                        print(f"  [ShipTo-GEO] Penalized ship-to {row['ship_to_id']} ({city}) "
                              f"— geo mismatch with PO delivery address ({addr_lower[:50]}...)")

            if best_score > 0.25:  # minimum confidence threshold
                result['ship_to_id']   = best_id
                result['ship_to_name'] = best_name
                return result

        # Final fallback: Default to Sold-To ID ONLY if NO delivery info at all
        # (no name, no address, no city AND no postcode was provided).
        if not combined_search and not ship_to_postcode:
            same_as_sold = ship_rows[ship_rows['ship_to_id'].astype(str).str.strip() == str(customer_id).strip()]
            if not same_as_sold.empty:
                r = same_as_sold.iloc[0]
                same_id   = str(r['ship_to_id']).strip()
                same_name = str(r.get('ship_to_name_full', '') or '').strip()
                print(f"  [ShipTo-BLANK] All delivery info blank -> defaulted to sold-to ID {same_id}")
                result['ship_to_id']   = same_id
                result['ship_to_name'] = same_name
                return result

        return result


    def _resolve_item_ship_to(self, customer_number: str, item: dict,
                               fallback_id: str, fallback_name: str) -> tuple:
        """
        Return (ship_to_id, ship_to_name) for a single line item.

        If the item carries its own ship-to info (extracted from a per-line
        'ship to' column on the PO), resolve it via Test MP.  Otherwise fall
        back to the header-level ship-to that was already resolved.
        """
        raw_name = str(item.get("line_ship_to_name", "") or "").strip()
        raw_addr = str(item.get("line_ship_to_address", "") or "").strip()
        raw_pc   = str(item.get("line_ship_to_postcode", "") or "").strip()

        if not (raw_name or raw_addr or raw_pc):
            # No per-line info — use header fallback
            return fallback_id, fallback_name

        mp = self.get_ship_to_info_from_test_mp(
            customer_id=customer_number,
            ship_to_id="",
            ship_to_address=raw_addr,
            ship_to_postcode=raw_pc,
            ship_to_name=raw_name,
        )
        resolved_id   = mp.get("ship_to_id")   or fallback_id
        resolved_name = mp.get("ship_to_name") or raw_name or fallback_name
        if resolved_id != fallback_id:
            try:
                print(f"  [ShipTo-Line] Item line_ship_to='{raw_name}' "
                      f"→ resolved {resolved_id} ({resolved_name})")
            except UnicodeEncodeError:
                print(f"  [ShipTo-Line] Per-line ship-to resolved: {resolved_id}")
        return resolved_id, resolved_name


    def generate_sales_orders(
        self,
        extracted_data: dict,
        filename_materials: Optional[List[str]] = None,
        email_text: str = '',
    ) -> List[Dict]:
        """
        Generate Sales Order payloads from enriched extracted data.

        Args:
            extracted_data:     dict with 'header_fields' and 'line_items'
            filename_materials: optional list of material IDs extracted from filename
                                (e.g. from "Material 14885" or "Materials 16552+47206")
            email_text:         full email body text — scanned by the decision tree
                                to detect 'consignment' keyword

        Returns:
            List of Sales Order dicts
        """
        header = extracted_data.get("header_fields", {})
        line_items = extracted_data.get("line_items", [])

        if not line_items:
            return []

        # Get customer number from KB enrichment
        # IMPORTANT: Use customer_number (the actual buyer) first, NOT vendor_id (which is Envalior's own ID)
        customer_number = str(header.get("customer_number") or header.get("vendor_id", "")).strip()
        if customer_number in CUSTOMER_ID_ALIASES:
            customer_number = CUSTOMER_ID_ALIASES[customer_number]
        po_number       = header.get("po_number", "UNKNOWN")

        # -- Resolve ship-to using Test MP (corrects wrong names from KB validation) --
        raw_ship_to_id   = str(header.get('ship_to_id', '') or '').strip()
        raw_ship_to_name = str(header.get('ship_to_name', '') or '').strip()
        ship_to_address  = str(header.get('ship_to_address', '') or '').strip()
        # Extract postal code from dedicated field (added to LLM schema) or parse from address
        ship_to_postcode = str(header.get('ship_to_postcode', '') or '').strip()
        if not ship_to_postcode and ship_to_address:
            # Fallback: try to parse Japanese-style 7-digit postcode or generic postcode from address
            _pc_match = re.search(r'\b(\d{3}-?\d{4}|\d{5,7})\b', ship_to_address)
            if _pc_match:
                ship_to_postcode = _pc_match.group(1).replace('-', '')

        ship_to_city = str(header.get('ship_to_city', '') or '').strip()
        # Auto-extract city from ship_to_address when the LLM did not provide it explicitly.
        # E.g. "Via L. Galvani 1, 24050 CALCINATE (BG), ITALY" → ship_to_city = "CALCINATE"
        # This is critical for postcode-disambiguation when multiple TestMP rows share a postcode area.
        if not ship_to_city and ship_to_postcode and ship_to_address:
            _pc_clean_for_city = ship_to_postcode.replace('-', '').replace(' ', '')
            _city_match = re.search(
                r'\b' + re.escape(_pc_clean_for_city) + r'\b[\s,]+([A-Z\u00c0-\u0178a-z\u00e0-\u00ff][A-Z\u00c0-\u0178a-z\u00e0-\u00ff\s-]{1,30}?)(?:\s*[\(,]|$)',
                ship_to_address, re.IGNORECASE
            )
            if _city_match:
                ship_to_city = _city_match.group(1).strip()
                print(f"  [ShipTo] Auto-extracted ship_to_city from address: '{ship_to_city}'")

        # Scan full PO text / email text for Incoterm delivery terms (e.g. 'CIP Freilassing')
        full_context = f"{ship_to_address} {extracted_data.get('raw_text', '')} {email_text}".strip()
        m_inco_ctx = re.search(r'\b(?:CIP|DAP|DDP|FCA|CPT|FOB|CIF|Lieferort|Delivery\s*to)\s*:?\s*([A-Z\u00c0-\u0178a-z\u00e0-\u00ff]{3,25})\b', full_context, re.IGNORECASE)
        if m_inco_ctx and m_inco_ctx.group(0) not in ship_to_address:
            ship_to_address = f"{ship_to_address} {m_inco_ctx.group(0)}".strip()

        mp_ship = self.get_ship_to_info_from_test_mp(
            customer_id=customer_number,
            ship_to_id=raw_ship_to_id,
            ship_to_address=ship_to_address,
            ship_to_postcode=ship_to_postcode,
            ship_to_name=raw_ship_to_name,
            ship_to_city=ship_to_city,
        )

        resolved_ship_to_id   = mp_ship['ship_to_id']   or raw_ship_to_id
        resolved_ship_to_name = mp_ship['ship_to_name'] or raw_ship_to_name

        if resolved_ship_to_id != raw_ship_to_id:
            try:
                print(f"  [ShipTo] Corrected ship_to_id via Test MP address match: "
                      f"{raw_ship_to_id} -> {resolved_ship_to_id} ({resolved_ship_to_name})")
            except UnicodeEncodeError:
                safe_name = str(resolved_ship_to_name).encode('ascii', 'ignore').decode()
                print(f"  [ShipTo] Corrected ship_to_id via Test MP address match: "
                      f"{raw_ship_to_id} -> {resolved_ship_to_id} ({safe_name})")
        elif resolved_ship_to_name and resolved_ship_to_name != raw_ship_to_name:
            try:
                print(f"  [ShipTo] Corrected ship_to_name via Test MP: "
                      f"'{raw_ship_to_name}' -> '{resolved_ship_to_name}' (id={resolved_ship_to_id})")
            except UnicodeEncodeError:
                safe_raw = str(raw_ship_to_name).encode('ascii', 'ignore').decode()
                safe_res = str(resolved_ship_to_name).encode('ascii', 'ignore').decode()
                print(f"  [ShipTo] Corrected ship_to_name via Test MP: "
                      f"'{safe_raw}' -> '{safe_res}' (id={resolved_ship_to_id})")

        # Try to get a default mapping for this customer
        default_mapping = self.lookup_by_customer_only(customer_number) or {}

        # ── customer_group2 -> SO-split rule ─────────────────────────────────
        # Z01          -> SEPARATE SO PER ITEM   (split_rule = 'O')
        # '-' or ''    -> ONE SO PER PO          (split_rule = 'X')  [default]
        cg2 = self._get_customer_group2(customer_number)
        if cg2 == 'Z01':
            cg2_label          = 'SEPARATE SO PER ITEM'
            default_split_rule = 'O'
        else:
            # '-' (explicit combined) or '' (not found) -> one Sales Order per PO
            cg2_label          = 'ONE SO PER PO'
            default_split_rule = 'X'
        print(f"  [Mapper] customer={customer_number}  customer_group2={cg2!r} "
              f"-> split_rule={default_split_rule}  ({cg2_label})")

        # Enrich each line item with Excel mapping
        enriched_items = []
        filename_materials = filename_materials or []

        for i, item in enumerate(line_items):
            mat_code = item.get("material_code", "")
            mat_desc = item.get("material_description", "")
            packaging = item.get("packaging", "")
            mapping = self.lookup(customer_number, mat_code, mat_desc, packaging=packaging)

            # Fallback: use material ID from filename (e.g. "MF Folien (Sold-to 4020001301 - Material 14885).pdf")
            if not mapping and i < len(filename_materials):
                hint = filename_materials[i]
                # Try as-is
                mapping = self.lookup(customer_number, hint, mat_desc, packaging=packaging)
                if not mapping and hint.isdigit():
                    # SAP internal material numbers are 18 digits zero-padded
                    padded18 = hint.zfill(18)
                    mapping = self.lookup(customer_number, padded18, mat_desc, packaging=packaging)
                if not mapping and hint.isdigit():
                    # Legacy: try 10-digit padding
                    padded10 = hint.zfill(10)
                    mapping = self.lookup(customer_number, padded10, mat_desc, packaging=packaging)
                # NOTE: We intentionally do NOT synthesise a material number from the filename
                # hint when the DB lookup fails. If the material isn't in the Celonis database
                # the PO must go to Exception POs so the CSR can add the missing mapping.
                if not mapping and hint.isdigit() and customer_number:
                    print(f"    [Mapper] Filename hint '{hint}' not found in database — leaving unmapped (Exception route).")

            # Fallback: LLM sometimes swaps material_code and material_description (e.g. Ergotech)
            if not mapping and mat_code and mat_desc and mat_code != mat_desc:
                mapping = self.lookup(customer_number, mat_desc, mat_code, packaging=packaging)

            # Convert unit + quantity together (handles LBS→KG, OZ→KG, MT→KG, T(US)→KG etc.)
            # is_us_format is computed below (line ~1594), but we need it here.
            # Determine US format early from sales org / address context.
            _first_mapping = mapping or default_mapping
            _tentative_sorg = str(_first_mapping.get("sales_organization", "") if _first_mapping else "")
            _addr_chk = (
                str(header.get("bill_to_address") or "") + " " +
                str(header.get("ship_to_address") or "")
            ).upper()
            _cust_chk = str(
                header.get("customer_name_matched") or header.get("customer_id_or_name") or ""
            ).upper()
            _is_us_item = (
                _tentative_sorg in ("2600", "2610", "2620", "2630")
                or "UNITED STATES" in _addr_chk
                or "U.S.A" in _addr_chk
                or "EVCO PLASTICS" in _cust_chk
                or any(st in _addr_chk for st in (" WI ", " TX ", " CA ", " OH ", " MI ", " NC ", " SC ", " GA ", " FL ", " PA ", " IN ", " MN ", " MO ", " IL "))
            )
            _sap_unit, _sap_qty = _normalise_unit_and_qty(
                item.get("unit", ""), item.get("quantity", ""), is_us_format=_is_us_item
            )
            enriched_item = {
                "internal_material_number": mapping.get("internal_material_number") if mapping else "",
                "extracted_material_number": mat_code,
                "material_description": item.get("material_description", ""),
                "customer_material_number": (
                    mapping.get("customer_material_number") if mapping
                    else item.get("customer_material_number", "")
                ),
                "quantity": _sap_qty,
                "unit":     _sap_unit,  # LBS→KG, OZ→KG, ISO KGM→KG, etc.
                "delivery_date": item.get("delivery_date", ""),
                "sales_organization": (
                    default_mapping.get("sales_organization", "")
                    if (mapping and mapping.get("is_global_fallback")) or not mapping
                    else mapping.get("sales_organization", "")
                ),
                "order_type": (
                    default_mapping.get("order_type", "")
                    if (mapping and mapping.get("is_global_fallback")) or not mapping
                    else mapping.get("order_type", "")
                ),
                "so_split_rule": (
                    mapping.get("so_split_rule") if mapping
                    else default_split_rule
                ),
            }
            # -- Order type determination via decision tree ----------------
            dt_order_type = None
            if self._decision_tree is not None:
                dt_result = self._decision_tree.determine(
                    customer_id       = customer_number or '',
                    material_internal = enriched_item.get('internal_material_number', '') or mat_code,
                    email_text        = email_text,
                    sales_organization= enriched_item.get('sales_organization', ''),
                )
                dt_order_type = dt_result.get('order_type')
                print(f"    [Item {i+1}] Decision Tree: order_type={dt_order_type} "
                      f"(confidence={dt_result.get('confidence')}, "
                      f"branch={dt_result.get('branch')})")

            # Decision tree result overrides Excel/Sheet2 order type when available
            if dt_order_type:
                enriched_item['order_type'] = dt_order_type
                enriched_item['order_type_reason'] = dt_result.get('reason', '')
                enriched_item['order_type_confidence'] = dt_result.get('confidence', '')
                enriched_item['order_type_branch'] = dt_result.get('branch', '')

            # ── Per-line ship-to resolution ────────────────────────────────
            # If this line item carried its own ship-to (from a per-line
            # 'ship to' column on the PO), resolve it now.  Store the result
            # directly on the enriched item so the SO grouping step can use it.
            item_st_id, item_st_name = self._resolve_item_ship_to(
                customer_number, item, resolved_ship_to_id, resolved_ship_to_name
            )
            enriched_item["_item_ship_to_id"]   = item_st_id
            enriched_item["_item_ship_to_name"] = item_st_name

            enriched_items.append(enriched_item)
            safe_mat_code = str(mat_code).encode('ascii', 'replace').decode('ascii')
            safe_mat_desc = str(mat_desc).encode('ascii', 'replace').decode('ascii')
            if mapping:
                print(f"    [Item {i+1}] MAPPED: '{safe_mat_code}' -> Internal '{enriched_item['internal_material_number']}'")
            else:
                print(f"    [Item {i+1}] NOT MAPPED: '{safe_mat_code}' (Desc: {safe_mat_desc[:30]}...)")

        # Auto-detect US date format for Americas SalesOrgs (2600/2610/2620/2630) or US addresses
        first_so = str(enriched_items[0].get("sales_organization", "") if enriched_items else "")
        cust_name_check = str(header.get("customer_name_matched") or header.get("customer_id_or_name") or "").upper()
        addr_check = (str(header.get("bill_to_address") or "") + " " + str(header.get("ship_to_address") or "")).upper()
        
        is_european_so = first_so in ("2500", "2540", "2545", "2100", "2200") or any(
            kw in cust_name_check or kw in addr_check
            for kw in ("ITALY", "ITALIA", "MILANO", "GERMANY", "DEUTSCHLAND", "FRANCE", "SPAIN", "NETHERLANDS", "ROMANIA")
        )
        is_us_date = False if is_european_so else (
            first_so in ("2600", "2610", "2620", "2630")
            or "UNITED STATES" in addr_check
            or "EVCO PLASTICS" in cust_name_check
            or "WI " in addr_check
        )

        from po_extraction_enhanced import _normalize_date
        order_date_norm = _normalize_date(header.get("order_date", ""), is_us_format=is_us_date)
        req_date_norm = _normalize_date(header.get("requested_delivery_date", ""), is_us_format=is_us_date)

        # Update header dict so downstream steps get DD/MM/YYYY dates
        header["order_date"] = order_date_norm
        header["requested_delivery_date"] = req_date_norm

        for item in enriched_items:
            if item.get("delivery_date"):
                item["delivery_date"] = _normalize_date(item["delivery_date"], is_us_format=is_us_date)

        # ──────────────────────────────────────────────────────────────────
        # Group items into Sales Orders
        # Priority:
        #   1. Multiple distinct ship-to IDs across items  → split by ship-to
        #      (PO number stays the same on every resulting SO)
        #   2. split_rule == 'O'                           → one SO per item
        #   3. split_rule == 'X' (default)                 → one SO for all items
        # ──────────────────────────────────────────────────────────────────
        sales_orders = []

        # Look up Employee Responsible & Email Responsible from Test MP / CSR Routing
        first_sorg = enriched_items[0].get("sales_organization", "") if enriched_items else ""
        emp_info   = self.get_salesperson_info(customer_number, first_sorg)
        emp_resp   = emp_info.get("employee_responsible", "")
        email_resp = emp_info.get("email_responsible", "")
        snd_email  = header.get("sender_email", "") or header.get("email_sender", "") or ""

        # -- Detect multiple ship-to parties --------------------------------
        unique_ship_to_ids = list(dict.fromkeys(
            item.get("_item_ship_to_id") or resolved_ship_to_id
            for item in enriched_items
        ))
        has_multiple_ship_tos = len(unique_ship_to_ids) > 1

        def _build_so_base(ship_to_id, ship_to_name, sorg, order_type,
                           split_rule_label, so_grouping_label):
            """Build the common SO dict fields shared by all split strategies."""
            return {
                "po_number":               po_number,
                "customer_number":         customer_number,
                "customer_name":           header.get("customer_name_matched") or header.get("customer_id_or_name", ""),
                "sales_organization":      sorg,
                "order_type":              order_type,
                "ship_to_id":              ship_to_id,
                "ship_to_name":            ship_to_name,
                "ship_to_source":          "per_line" if has_multiple_ship_tos else "header",
                "sold_to_id":              header.get("sold_to_id") or customer_number,
                "employee_responsible":    emp_resp,
                "email_responsible":       email_resp,
                "sender_email":            snd_email,
                "order_date":              order_date_norm,
                "requested_delivery_date": req_date_norm,
                "customer_group2":         cg2,
                "so_grouping_label":       so_grouping_label,
                "so_split_rule":           split_rule_label,
            }

        def _clean_item(it):
            """Strip internal/routing keys before embedding item into SO payload."""
            skip = {"so_split_rule", "sales_organization", "order_type",
                    "_item_ship_to_id", "_item_ship_to_name"}
            return {k: v for k, v in it.items() if k not in skip}

        if has_multiple_ship_tos:
            # ── Strategy 1: split by ship-to group ────────────────────────
            # Group items by their resolved ship-to ID while preserving order.
            # Items without their own ship-to fall into the header ship-to group.
            print(f"  [ShipTo-Split] PO has {len(unique_ship_to_ids)} distinct ship-to IDs "
                  f"→ creating {len(unique_ship_to_ids)} SO(s): {unique_ship_to_ids}")

            from collections import OrderedDict
            groups: OrderedDict = OrderedDict()
            for it in enriched_items:
                st_id   = it.get("_item_ship_to_id")   or resolved_ship_to_id
                st_name = it.get("_item_ship_to_name") or resolved_ship_to_name
                if st_id not in groups:
                    groups[st_id] = {"ship_to_name": st_name, "items": []}
                groups[st_id]["items"].append(it)

            for st_id, grp in groups.items():
                grp_items = grp["items"]
                s_sorg     = grp_items[0].get("sales_organization", "")
                s_otype    = grp_items[0].get("order_type", "")
                so = _build_so_base(
                    ship_to_id       = st_id,
                    ship_to_name     = grp["ship_to_name"],
                    sorg             = s_sorg,
                    order_type       = s_otype,
                    split_rule_label = "S",   # 'S' = split by Ship-To
                    so_grouping_label= "SPLIT BY SHIP-TO",
                )
                so["items"] = [_clean_item(it) for it in grp_items]
                sales_orders.append(so)

        else:
            # -- Check if ALL items say "X" (one SO per PO) ----------------
            all_x = all(item.get("so_split_rule", "X") == "X" for item in enriched_items)

            if all_x:
                # ── Strategy 3: one SO for all items ──────────────────────
                so = _build_so_base(
                    ship_to_id       = resolved_ship_to_id,
                    ship_to_name     = resolved_ship_to_name,
                    sorg             = first_sorg,
                    order_type       = enriched_items[0].get("order_type", ""),
                    split_rule_label = "X",
                    so_grouping_label= cg2_label or "ONE SO PER PO",
                )
                so["items"] = [_clean_item(it) for it in enriched_items]
                sales_orders.append(so)
            else:
                # ── Strategy 2: one SO per item ───────────────────────────
                for item in enriched_items:
                    s_sorg = item.get("sales_organization", "")
                    s_emp  = self.get_salesperson_info(customer_number, s_sorg)
                    so = _build_so_base(
                        ship_to_id       = resolved_ship_to_id,
                        ship_to_name     = resolved_ship_to_name,
                        sorg             = s_sorg,
                        order_type       = item.get("order_type", ""),
                        split_rule_label = "O",
                        so_grouping_label= cg2_label or "SEPARATE SO PER ITEM",
                    )
                    so["employee_responsible"] = s_emp.get("employee_responsible", emp_resp)
                    so["email_responsible"]    = s_emp.get("email_responsible", email_resp)
                    so["items"] = [_clean_item(item)]
                    sales_orders.append(so)

        return sales_orders



# -- Quick test --
if __name__ == "__main__":
    mapper = SalesOrderMapper("EXPORT_20260216_100124.XLSX")

    # Test lookup
    test_result = mapper.lookup("4020004601", "0000011886")
    print(f"\nTest lookup (4020004601, 0000011886): {test_result}")

    # Test with sample extracted data
    sample = {
        "header_fields": {
            "po_number": "TEST-001",
            "vendor_id": "4020004601",
            "order_date": "01/01/2024",
            "ship_to_id": "SH001",
            "sold_to_id": "SO001",
        },
        "line_items": [
            {"material_code": "0000011886", "material_description": "Test Material", "quantity": "500", "unit": "KG"},
        ]
    }
    orders = mapper.generate_sales_orders(sample)
    import json
    print(f"\nGenerated {len(orders)} Sales Order(s):")
    print(json.dumps(orders, indent=2))
