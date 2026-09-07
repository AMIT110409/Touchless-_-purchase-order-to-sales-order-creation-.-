#!/usr/bin/env python
"""
exception_resolver.py
=====================
Batch Exception & Mapping Resolution Pipeline
----------------------------------------------
Loads ALL reference data ONCE (Celonis CMIR, order-mapping, DB emails),
then processes ALL exception / unprocessed POs in a single pass.

Problems it finds and fixes automatically:
  ✅ Status stuck at FOLDER_MOVED / EXCEPTION_ROUTED / MAPPING_FAILED  → reset to PENDING
  ✅ Customer name dedup (e.g. "Pegasus … Pegasus …")                   → fixed in runtime index
  ✅ Low fuzzy-match score due to duplicate name in CMIR                → dedup applied before matching
  ✅ Customer material not in CMIR                                       → tries material-desc fallback
  ✅ Material description mismatch (color code variation)                → partial-token match
  ✅ CSR email missing (no salesperson_email in CMIR)                   → routes to regional mailbox
  ✅ Multiple POs from same customer reuse the same loaded CMIR slice    → zero extra API calls

Output:
  - exception_resolver_report.xlsx   : Full analysis per PO item
  - exception_resolver_actions.json  : Machine-readable list of actions taken
  - Resets eligible DB rows to PENDING so next pipeline run retries them

Usage:
  python exception_resolver.py                  # live mode
  python exception_resolver.py --dry-run        # report only, no DB changes
  python exception_resolver.py --reset-all      # reset ALL non-terminal stuck rows
  python exception_resolver.py --po PO-1234     # single PO debug

  Optional filters:
  --customer "Pegasus"       # only process POs for this customer
  --status EXCEPTION_ROUTED  # only rows with this DB status
  --refresh-cache            # force re-download Celonis data before running
"""

import os
import re
import sys
import json
import sqlite3
import argparse
import warnings
from datetime import datetime
from pathlib import Path
from difflib import SequenceMatcher
from typing import Optional

# ── suppress pandas performance warnings ──────────────────────────────────────
warnings.filterwarnings("ignore", category=FutureWarning)

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Output files ──────────────────────────────────────────────────────────────
REPORT_XLSX  = BASE_DIR / "exception_resolver_report.xlsx"
ACTIONS_JSON = BASE_DIR / "exception_resolver_actions.json"
DB_PATH      = BASE_DIR / "processed_emails.db"

# ── Statuses we will attempt to fix (reset to PENDING) ────────────────────────
FIXABLE_STATUSES = {
    "FOLDER_MOVED",
    "EXCEPTION_ROUTED",
    "MAPPING_FAILED",
    "EXTRACTION_FAILED",
    "SO_CREATION_FAILED",
    "PENDING",
}

# ── Statuses that are truly terminal (never reset) ────────────────────────────
TERMINAL_STATUSES = {
    "ROBONA_SENT", "PUSHED_TO_CELONIS", "SO_CREATED",
    "PENDING_STAGE2", "SKIP", "ARCHIVED", "NO_EMAIL_ID",
}

# ─────────────────────────────────────────────────────────────────────────────
# Section 1: Data Loaders  (called ONCE per run)
# ─────────────────────────────────────────────────────────────────────────────

def _strip_dup_name(name: str) -> str:
    """
    Fix Celonis export artefact where Name1+Name2 are identical, producing
    'Pegasus Polymers Pte. Ltd. Pegasus Polymers Pte. Ltd.'.
    Also handles partially-duplicated names like 'X Y Z X Y Z Something'.
    """
    s = str(name).strip()
    # Exact half-duplicate (even length)
    n = len(s)
    for half in range(max(5, n // 3), n // 2 + 1):
        first = s[:half].rstrip()
        rest  = s[half:].lstrip()
        if rest.lower().startswith(first.lower()):
            tail = rest[len(first):].strip()
            return (first + " " + tail).strip() if tail else first
    return s


def load_cmir(force_refresh: bool = False):
    """Load Customer Master (CMIR) — uses cache if fresh (< 24h)."""
    import pandas as pd
    from azure_table_reader import load_customer_master
    print("  [CMIR] Loading Customer Master (CMIR)…", end=" ", flush=True)
    df = load_customer_master(force_refresh=force_refresh)
    # Fix duplicated company names before building the index
    for col in ("sold_to_name_full", "ship_to_name_full"):
        if col in df.columns:
            df[col] = df[col].apply(_strip_dup_name)
    print(f"{len(df):,} rows loaded.")
    return df


def load_order_mapping(force_refresh: bool = False):
    """Load Sheet3 / Order-Type mapping from cache."""
    import pandas as pd
    cache = BASE_DIR / ".celonis_cache" / "sheet3_order_mapping.parquet"
    if cache.exists():
        print("  [ORDER-MAP] Using cached order mapping.", flush=True)
        return pd.read_parquet(cache)
    print("  [ORDER-MAP] Cache not found — skipping order-type mapping.", flush=True)
    return pd.DataFrame()


def load_db_emails(filter_status: Optional[str] = None,
                   filter_customer: Optional[str] = None) -> list[dict]:
    """Load all emails from processed_emails.db that need attention."""
    if not DB_PATH.exists():
        print("  [DB] processed_emails.db not found.")
        return []
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    sql = (
        "SELECT message_id, subject, status, stage, error_log, "
        "       attachments, so_number, block_code, block_reason "
        "FROM processed_emails"
    )
    clauses, params = [], []
    if filter_status:
        clauses.append("status = ?")
        params.append(filter_status)
    else:
        placeholders = ",".join("?" * len(FIXABLE_STATUSES))
        clauses.append(f"status IN ({placeholders})")
        params.extend(list(FIXABLE_STATUSES))
    if filter_customer:
        clauses.append("(subject LIKE ? OR attachments LIKE ?)")
        params += [f"%{filter_customer}%", f"%{filter_customer}%"]
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    cur.execute(sql, params)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    print(f"  [DB] {len(rows)} email rows loaded from DB.")
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Section 2: CMIR Index  (built ONCE, reused for every PO)
# ─────────────────────────────────────────────────────────────────────────────

class CMIRIndex:
    """
    In-memory index built from the CMIR DataFrame.
    Provides O(1)-ish customer lookup and fast material matching.
    Built once per run — all POs share the same instance.
    """

    def __init__(self, df):
        import pandas as pd
        self.df = df
        # Build name → sold_to_id lookup (lower-cased)
        self._name_map: dict[str, str] = {}
        for _, row in df[["sold_to_id","sold_to_name_full"]].drop_duplicates().iterrows():
            sid  = str(row["sold_to_id"]).strip()
            name = str(row["sold_to_name_full"]).strip()
            if name and sid:
                self._name_map[name.lower()] = sid

        # Pre-group by sold_to_id for fast material lookup
        self._by_customer: dict[str, object] = {
            sid: grp for sid, grp in df.groupby("sold_to_id")
        }

        # Build customer_material_number → (sold_to_id, material_internal) lookup
        self._cmat_map: dict[str, list] = {}
        for _, row in df[df["customer_material_number"].astype(str).str.strip().ne("")].iterrows():
            cm = str(row["customer_material_number"]).strip().upper()
            if cm:
                self._cmat_map.setdefault(cm, []).append({
                    "sold_to_id": row["sold_to_id"],
                    "material_internal": row["material_internal"],
                    "material_description": row.get("material_description",""),
                    "sales_organization": row.get("sales_organization",""),
                    "salesperson_email": row.get("salesperson_email",""),
                })

    def _normalize(self, s: str) -> str:
        s = str(s).lower().strip()
        s = re.sub(r'\bpte\.?\s*ltd\.?\b', 'pte ltd', s)
        s = re.sub(r'\bsdn\.?\s*bhd\.?\b', 'sdn bhd', s)
        s = re.sub(r'\bgmbh\.?\b', 'gmbh', s)
        s = re.sub(r'\bco\.?\s*ltd\.?\b', 'co ltd', s)
        s = re.sub(r'\binc\.?\b', 'inc', s)
        s = re.sub(r'\bllc\.?\b', 'llc', s)
        s = re.sub(r'\bltd\.?\b', 'ltd', s)
        s = re.sub(r'\bcorp\.?\b', 'corp', s)
        s = re.sub(r'[^a-z0-9\s]', ' ', s)
        return re.sub(r'\s+', ' ', s).strip()

    # ── Customer lookup ─────────────────────────────────────────────────────

    def find_customer(self, name: str, threshold: float = 0.72) -> Optional[dict]:
        """
        Fuzzy-match a company name against CMIR.
        Returns best matching sold_to_id + metadata, or None.
        """
        norm_q = self._normalize(name)
        best_score, best_sid = 0.0, None
        for cmir_name, sid in self._name_map.items():
            norm_c = self._normalize(cmir_name)
            score  = SequenceMatcher(None, norm_q, norm_c).ratio()
            # Boost if one contains the other
            if norm_q in norm_c or norm_c in norm_q:
                score = max(score, 0.85)
            if score > best_score:
                best_score, best_sid = score, sid
        if best_score >= threshold and best_sid:
            grp = self._by_customer.get(best_sid)
            return {
                "sold_to_id":       best_sid,
                "match_score":      round(best_score, 3),
                "name_in_cmir":     grp["sold_to_name_full"].iloc[0] if grp is not None else "",
                "sales_orgs":       list(grp["sales_organization"].unique()) if grp is not None else [],
                "salesperson_email":self._get_csr(best_sid),
            }
        return None

    def _get_csr(self, sold_to_id: str) -> str:
        grp = self._by_customer.get(sold_to_id)
        if grp is None:
            return ""
        emails = grp["salesperson_email"].dropna().unique()
        if len(emails) == 1:
            return str(emails[0]).strip()
        # Multiple CSRs — return comma-separated
        return ", ".join(str(e).strip() for e in emails if str(e).strip())

    # ── Material lookup ─────────────────────────────────────────────────────

    def find_material(self, sold_to_id: str,
                      customer_material_number: str = "",
                      material_description: str = "",
                      internal_material: str = "") -> dict:
        """
        3-pass material lookup for a given customer:
          1. Customer material number (exact)
          2. Internal material number (exact, leading-zero normalised)
          3. Material description (partial-token fuzzy)
        Returns a dict with match info.
        """
        result = {
            "matched": False,
            "match_method": None,
            "material_internal": None,
            "material_description_cmir": None,
            "customer_material_number_cmir": None,
            "issue": None,
        }

        grp = self._by_customer.get(str(sold_to_id).strip())
        if grp is None:
            result["issue"] = "Customer not found in CMIR index"
            return result

        # Helper: clean to alphanumeric lowercase for robust code comparison
        def _clean(s: str) -> str:
            return re.sub(r'[^a-z0-9]', '', str(s).lower())

        # ── Pass 1: Customer material number (exact / slash-normalized) ──────
        cm_raw  = str(customer_material_number).strip().upper()
        cm_norm = cm_raw.lstrip("0")
        cm_clean = _clean(cm_raw)

        if cm_clean:
            for _, row in grp.iterrows():
                row_cmat = str(row.get("customer_material_number", "")).strip().upper()
                if not row_cmat:
                    continue
                row_clean = _clean(row_cmat)
                if row_clean == cm_clean or (len(row_clean) >= 4 and (row_clean in cm_clean or cm_clean in row_clean)):
                    result.update({
                        "matched": True,
                        "match_method": "customer_material_exact",
                        "material_internal": row["material_internal"],
                        "material_description_cmir": row.get("material_description",""),
                        "customer_material_number_cmir": row.get("customer_material_number",""),
                    })
                    return result

        # ── Pass 2: Internal material number (zero-padded or plain) ─────────
        if internal_material:
            im_norm = str(internal_material).strip().lstrip("0")
            sub = grp[
                grp["material_internal"].astype(str).str.strip().str.lstrip("0") == im_norm
            ]
            if not sub.empty:
                row = sub.iloc[0]
                result.update({
                    "matched": True,
                    "match_method": "material_internal_exact",
                    "material_internal": row["material_internal"],
                    "material_description_cmir": row.get("material_description",""),
                    "customer_material_number_cmir": row.get("customer_material_number",""),
                })
                return result

        # ── Pass 3: CMIR Customer Material contained in PO Description ────────
        # Handles legacy/old item codes on PO (e.g. Seco PO using 'ARNITE T06200' as code
        # while description contains 'POCAN B1906 650153' or 'T06200D/S2.06.22')
        if material_description:
            clean_po_desc = _clean(material_description)
            for _, row in grp.iterrows():
                row_cmat = str(row.get("customer_material_number", "")).strip()
                clean_row_cmat = _clean(row_cmat)
                if len(clean_row_cmat) >= 4 and clean_row_cmat in clean_po_desc:
                    result.update({
                        "matched": True,
                        "match_method": f"cmat_in_desc ({row_cmat})",
                        "material_internal": row["material_internal"],
                        "material_description_cmir": row.get("material_description",""),
                        "customer_material_number_cmir": row.get("customer_material_number",""),
                    })
                    return result

        # ── Pass 4: Description token fuzzy match ────────────────────────────
        if material_description:
            best_score, best_row = 0.0, None
            po_desc_norm = re.sub(r"[^a-z0-9]", " ",
                                  material_description.lower()).split()

            for _, row in grp.iterrows():
                cmir_desc = str(row.get("material_description","")).strip()
                if not cmir_desc:
                    continue
                cmir_tokens = re.sub(r"[^a-z0-9]", " ", cmir_desc.lower()).split()
                # Jaccard over tokens
                s_po   = set(po_desc_norm)
                s_cmir = set(cmir_tokens)
                inter  = s_po & s_cmir
                union  = s_po | s_cmir
                jaccard = len(inter) / len(union) if union else 0
                # Also run SequenceMatcher on raw strings
                seq = SequenceMatcher(None, material_description.lower(),
                                      cmir_desc.lower()).ratio()
                score = 0.6 * jaccard + 0.4 * seq
                if score > best_score:
                    best_score = score
                    best_row   = row

            if best_score >= 0.35 and best_row is not None:
                result.update({
                    "matched": True,
                    "match_method": f"desc_fuzzy ({best_score:.2f})",
                    "material_internal": best_row["material_internal"],
                    "material_description_cmir": best_row.get("material_description",""),
                    "customer_material_number_cmir": best_row.get("customer_material_number",""),
                })
                return result

        # ── Nothing matched ─────────────────────────────────────────────────
        result["issue"] = (
            f"No match: cust_mat={customer_material_number!r}, "
            f"int_mat={internal_material!r}, "
            f"desc={material_description!r}"
        )
        return result


# ─────────────────────────────────────────────────────────────────────────────
# Section 3: PO Data Loader  (read extracted JSONL results)
# ─────────────────────────────────────────────────────────────────────────────

def _extract_fields(record: dict) -> dict:
    """
    Normalise the raw JSONL record into a flat dict regardless of whether
    the record came from the old format (top-level keys) or the new enriched
    format (header_fields / line_items / sales_orders).
    """
    hdr = record.get("header_fields") or {}
    # Company name — try multiple locations
    company = (
        str(hdr.get("company_name", "") or hdr.get("customer_name", ""))
        or str(record.get("company_name", "") or record.get("customer_name", ""))
    ).strip()

    # PO number
    po_num = (
        str(hdr.get("po_number", "") or record.get("po_number", ""))
    ).strip()

    # Line items — try line_items first, then items
    raw_items = record.get("line_items") or record.get("items") or []
    # Also pull from sales_orders if no line_items
    if not raw_items:
        for so in (record.get("sales_orders") or []):
            raw_items.extend(so.get("items") or [])

    # Normalise each item to have consistent field names
    items = []
    for it in raw_items:
        if not isinstance(it, dict):
            continue
        items.append({
            "material_description":     str(it.get("material_description","") or
                                            it.get("description","") or "").strip(),
            "customer_material_number": str(it.get("customer_material_number","") or
                                            it.get("customer_material","") or "").strip(),
            "material_number":          str(it.get("material_number","") or
                                            it.get("material_internal","") or
                                            it.get("sap_material","") or "").strip(),
            "quantity":                 it.get("quantity",""),
            "unit":                     it.get("unit",""),
        })

    return {
        "company_name": company,
        "po_number":    po_num,
        "items":        items,
        "source_file":  str(record.get("source_file", "") or ""),
        "_raw":         record,  # keep original for debugging
    }


def load_extracted_pos(filter_customer: Optional[str] = None,
                       filter_po: Optional[str] = None) -> list[dict]:
    """
    Load PO items from results_unprocessed_run_enriched.jsonl and similar
    result files that contain the OCR-extracted + enriched PO data.
    Falls back to results_unprocessed_run.jsonl if enriched not found.
    """
    # Priority order for result files
    candidates = [
        "results_unprocessed_run_enriched.jsonl",
        "results_unprocessed_run.jsonl",
        "results_outlook_all_emails_enriched.jsonl",
        "results_PO examples_enriched.jsonl",
    ]
    raw_records = []
    loaded_from = None
    for fname in candidates:
        fp = BASE_DIR / fname
        if fp.exists() and fp.stat().st_size > 100:
            try:
                with open(fp, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            raw_records.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
                loaded_from = fname
                break
            except Exception as e:
                print(f"  [WARN] Could not read {fname}: {e}")

    if not raw_records:
        print("  [PO] No extracted PO result files found.")
        return []

    # Normalise all records to flat format
    records = [_extract_fields(r) for r in raw_records]

    # Filter out records with no useful content
    records = [r for r in records if r["company_name"] or r["po_number"] or r["items"]]
    print(f"  [PO] Loaded {len(records):,} usable PO records from {loaded_from}")

    # Apply filters
    if filter_po:
        records = [r for r in records
                   if filter_po.lower() in r["po_number"].lower()]
    if filter_customer:
        records = [r for r in records
                   if filter_customer.lower() in r["company_name"].lower()]
    if filter_po or filter_customer:
        print(f"  [PO] After filter: {len(records)} records")
    return records


# ─────────────────────────────────────────────────────────────────────────────
# Section 4: Core Resolution Logic  (runs on each PO item)
# ─────────────────────────────────────────────────────────────────────────────

def resolve_po_record(record: dict, idx: CMIRIndex) -> dict:
    """
    Analyse a single PO record and return a resolution report row.
    This is purely analytical — no side effects.
    Record must already be normalised by _extract_fields().
    """
    company  = str(record.get("company_name","")).strip()
    po_num   = str(record.get("po_number","")).strip()
    source   = str(record.get("source_file","")).strip()
    items    = record.get("items") or []

    res = {
        "po_number":          po_num,
        "company_name_po":    company,
        "source_file":        source,
        "company_match":      "NONE",
        "sold_to_id":         "",
        "name_in_cmir":       "",
        "match_score":        0.0,
        "sales_orgs":         "",
        "csr_email":          "",
        "total_items":        len(items) if isinstance(items, list) else 1,
        "items_mapped":       0,
        "items_missing":      0,
        "actions":            [],
        "item_details":       [],
    }

    # ── Step 1: Customer match ─────────────────────────────────────────────
    cust = idx.find_customer(company) if company else None
    if cust:
        res["company_match"]  = "MATCHED"
        res["sold_to_id"]     = cust["sold_to_id"]
        res["name_in_cmir"]   = cust["name_in_cmir"]
        res["match_score"]    = cust["match_score"]
        res["sales_orgs"]     = ", ".join(str(s) for s in cust.get("sales_orgs",[]))
        res["csr_email"]      = cust.get("salesperson_email","")
    else:
        res["company_match"] = "NOT_FOUND"
        res["actions"].append(f"CUSTOMER_NOT_FOUND: '{company}' – needs manual CMIR lookup")

    # ── Step 2: Material match for each line item ─────────────────────────
    if not isinstance(items, list):
        items = [items] if items else []

    mapped, missing = 0, 0
    item_details = []

    for item in items:
        if not isinstance(item, dict):
            continue
        desc      = str(item.get("material_description","")).strip()
        cust_mat  = str(item.get("customer_material_number","")).strip()
        int_mat   = str(item.get("material_number","")         # mapped field
                   or item.get("material_internal","")
                   or item.get("sap_material","")).strip()

        mat_result = {"item_desc": desc, "cust_mat": cust_mat, "int_mat": int_mat}

        if cust:
            mres = idx.find_material(
                sold_to_id=cust["sold_to_id"],
                customer_material_number=cust_mat,
                material_description=desc,
                internal_material=int_mat,
            )
            mat_result.update(mres)
            if mres["matched"]:
                mapped += 1
                mat_result["status"] = "MAPPED"
            else:
                missing += 1
                mat_result["status"] = "MISSING"
                res["actions"].append(
                    f"MATERIAL_MISSING: '{desc}' (cust_mat={cust_mat}) "
                    f"for customer {cust['sold_to_id']} — {mres['issue']}"
                )
        else:
            missing += 1
            mat_result["status"] = "SKIPPED_NO_CUSTOMER"

        item_details.append(mat_result)

    res["items_mapped"]  = mapped
    res["items_missing"] = missing
    res["item_details"]  = item_details

    # ── Step 3: CSR routing ────────────────────────────────────────────────
    if not res["csr_email"]:
        res["actions"].append("CSR_EMAIL_MISSING: No salesperson_email in CMIR — will route to regional mailbox")

    return res


# ─────────────────────────────────────────────────────────────────────────────
# Section 5: DB Reset  (reset fixable stuck emails to PENDING)
# ─────────────────────────────────────────────────────────────────────────────

def reset_stuck_emails(dry_run: bool = True,
                       filter_status: Optional[str] = None,
                       filter_customer: Optional[str] = None) -> list[dict]:
    """
    Find emails stuck in FIXABLE_STATUSES and reset to PENDING
    so the next pipeline run retries them.
    """
    if not DB_PATH.exists():
        print("  [DB] processed_emails.db not found.")
        return []

    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()

    target_statuses = (
        {filter_status} if filter_status and filter_status in FIXABLE_STATUSES
        else FIXABLE_STATUSES
    )

    placeholders = ",".join("?" * len(target_statuses))
    sql = (
        f"SELECT message_id, subject, status, attachments "
        f"FROM processed_emails WHERE status IN ({placeholders})"
    )
    params = list(target_statuses)

    if filter_customer:
        sql += " AND (subject LIKE ? OR attachments LIKE ?)"
        params += [f"%{filter_customer}%", f"%{filter_customer}%"]

    cur.execute(sql, params)
    rows = cur.fetchall()

    actions = []
    for msg_id, subject, status, attachments in rows:
        action = {
            "msg_id":      msg_id[:30] + "…" if len(msg_id) > 30 else msg_id,
            "subject":     subject,
            "old_status":  status,
            "new_status":  "PENDING",
            "attachments": attachments,
            "dry_run":     dry_run,
        }
        actions.append(action)
        if not dry_run:
            cur.execute(
                "UPDATE processed_emails SET status='PENDING', "
                "updated_at=CURRENT_TIMESTAMP WHERE message_id=?",
                (msg_id,)
            )

    if not dry_run:
        conn.commit()
    conn.close()

    label = "[DRY-RUN]" if dry_run else "[RESET]"
    print(f"  [DB] {label} {len(actions)} email(s) {'would be' if dry_run else 'were'} reset to PENDING.")
    return actions


# ─────────────────────────────────────────────────────────────────────────────
# Section 6: Report Writer
# ─────────────────────────────────────────────────────────────────────────────

def write_report(resolutions: list[dict], reset_actions: list[dict],
                 dry_run: bool = True):
    """Write Excel + JSON report."""
    try:
        import pandas as pd
        from openpyxl.styles import PatternFill, Font, Alignment
        from openpyxl.utils import get_column_letter

        # ── Sheet 1: Summary per PO ───────────────────────────────────────
        summary_rows = []
        for r in resolutions:
            summary_rows.append({
                "PO Number":       r["po_number"],
                "Company (PO)":    r["company_name_po"],
                "Source File":     r.get("source_file",""),
                "Company Match":   r["company_match"],
                "Sold-To ID":      r["sold_to_id"],
                "Name in CMIR":    r["name_in_cmir"],
                "Match Score":     r["match_score"],
                "Sales Orgs":      r["sales_orgs"],
                "CSR Email":       r["csr_email"],
                "Total Items":     r["total_items"],
                "Items Mapped":    r["items_mapped"],
                "Items Missing":   r["items_missing"],
                "Actions":         " | ".join(r["actions"]) if r["actions"] else "OK",
            })

        # ── Sheet 2: Item-level detail ────────────────────────────────────
        item_rows = []
        for r in resolutions:
            for it in r.get("item_details", []):
                item_rows.append({
                    "PO Number":              r["po_number"],
                    "Company (PO)":           r["company_name_po"],
                    "Sold-To ID":             r["sold_to_id"],
                    "Item Description (PO)":  it.get("item_desc",""),
                    "Cust Mat (PO)":          it.get("cust_mat",""),
                    "Int Mat (PO)":           it.get("int_mat",""),
                    "Status":                 it.get("status",""),
                    "Match Method":           it.get("match_method",""),
                    "Material (CMIR)":        it.get("material_internal",""),
                    "Description (CMIR)":     it.get("material_description_cmir",""),
                    "Cust Mat (CMIR)":        it.get("customer_material_number_cmir",""),
                    "Issue":                  it.get("issue",""),
                })

        # ── Sheet 3: DB Reset Actions ─────────────────────────────────────
        reset_rows = []
        for a in reset_actions:
            reset_rows.append({
                "Message ID (truncated)": a["msg_id"],
                "Subject":                a["subject"],
                "Old Status":             a["old_status"],
                "New Status":             a["new_status"] if not a["dry_run"] else "PENDING (dry-run)",
                "Attachments":            a["attachments"],
            })

        df_summary = pd.DataFrame(summary_rows)
        df_items   = pd.DataFrame(item_rows)
        df_resets  = pd.DataFrame(reset_rows)

        with pd.ExcelWriter(REPORT_XLSX, engine="openpyxl") as writer:
            df_summary.to_excel(writer, sheet_name="PO Summary",    index=False)
            df_items.to_excel(  writer, sheet_name="Item Detail",   index=False)
            df_resets.to_excel( writer, sheet_name="DB Resets",     index=False)

            # Basic styling
            wb = writer.book
            RED   = PatternFill("solid", fgColor="FFCCCC")
            GREEN = PatternFill("solid", fgColor="CCFFCC")
            AMBER = PatternFill("solid", fgColor="FFEECC")

            for sheet_name, df in [("PO Summary", df_summary),
                                    ("Item Detail", df_items)]:
                ws = wb[sheet_name]
                # Header row bold
                for cell in ws[1]:
                    cell.font = Font(bold=True)
                    cell.alignment = Alignment(wrap_text=True)
                # Colour rows by status
                status_col = None
                for i, col_name in enumerate(df.columns, 1):
                    if col_name in ("Company Match", "Status"):
                        status_col = i
                        break
                if status_col:
                    for row in ws.iter_rows(min_row=2):
                        val = str(row[status_col - 1].value or "")
                        fill = (
                            GREEN if val in ("MATCHED","MAPPED","OK")
                            else RED   if val in ("NOT_FOUND","MISSING")
                            else AMBER
                        )
                        for cell in row:
                            cell.fill = fill
                # Auto column width
                for col_idx, col in enumerate(df.columns, 1):
                    max_len = max(len(str(col)),
                                  df.iloc[:, col_idx-1].astype(str).str.len().max() if len(df) else 0)
                    ws.column_dimensions[get_column_letter(col_idx)].width = min(max_len + 4, 50)

        print(f"\n  📊 Report written → {REPORT_XLSX}")

    except ImportError as e:
        print(f"  [WARN] Excel report skipped (missing lib): {e}")

    # Always write JSON actions
    all_actions = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "dry_run": dry_run,
        "po_resolutions": resolutions,
        "db_resets": reset_actions,
    }
    with open(ACTIONS_JSON, "w", encoding="utf-8") as f:
        json.dump(all_actions, f, indent=2, default=str)
    print(f"  📄 Actions JSON  → {ACTIONS_JSON}")


# ─────────────────────────────────────────────────────────────────────────────
# Section 7: Main
# ─────────────────────────────────────────────────────────────────────────────

def _sec(title: str):
    print()
    print("=" * 70)
    print(f"  {title}")
    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(
        description="Batch Exception & Mapping Resolver — processes ALL POs in ONE pass."
    )
    parser.add_argument("--dry-run",      action="store_true",
                        help="Analyse only — do NOT update DB statuses.")
    parser.add_argument("--reset-all",    action="store_true",
                        help="Reset ALL fixable stuck rows to PENDING.")
    parser.add_argument("--refresh-cache",action="store_true",
                        help="Force re-download Celonis data before running.")
    parser.add_argument("--customer",     type=str, default=None,
                        help="Filter: only process POs for this customer name.")
    parser.add_argument("--status",       type=str, default=None,
                        help="Filter: only DB rows with this status (e.g. FOLDER_MOVED).")
    parser.add_argument("--po",           type=str, default=None,
                        help="Filter: only this PO number.")
    parser.add_argument("--no-report",    action="store_true",
                        help="Skip Excel/JSON report generation.")
    args = parser.parse_args()

    print("\n" + "━"*70)
    print("  🔧 Envalior Touchless Order — Batch Exception Resolver")
    print(f"     Mode: {'DRY-RUN (no DB changes)' if args.dry_run else 'LIVE'}")
    print("━"*70)

    # ── Load ALL data ONCE ──────────────────────────────────────────────────
    _sec("Step 1/5 — Loading Reference Data  (once per run)")
    cmir_df = load_cmir(force_refresh=args.refresh_cache)
    load_order_mapping(force_refresh=args.refresh_cache)  # future use

    # ── Build index ONCE ────────────────────────────────────────────────────
    _sec("Step 2/5 — Building In-Memory CMIR Index")
    idx = CMIRIndex(cmir_df)
    print(f"  [INDEX] {len(idx._name_map):,} unique customer names indexed.")
    print(f"  [INDEX] {len(idx._by_customer):,} unique sold-to IDs.")
    print(f"  [INDEX] {sum(len(v) for v in idx._cmat_map.values()):,} customer material entries.")

    # ── Load PO extract results ─────────────────────────────────────────────
    _sec("Step 3/5 — Loading Extracted PO Records")
    po_records = load_extracted_pos(
        filter_customer=args.customer,
        filter_po=args.po,
    )

    # ── Resolve ALL POs ─────────────────────────────────────────────────────
    _sec("Step 4/5 — Resolving POs  (all customers in single pass)")
    resolutions = []
    counts = {"MATCHED": 0, "NOT_FOUND": 0, "items_ok": 0, "items_missing": 0}

    for i, rec in enumerate(po_records, 1):
        res = resolve_po_record(rec, idx)
        resolutions.append(res)
        counts[res["company_match"]] = counts.get(res["company_match"], 0) + 1
        counts["items_ok"]      += res["items_mapped"]
        counts["items_missing"] += res["items_missing"]

        # Print live status for each PO
        icon = "✅" if res["company_match"] == "MATCHED" else "❌"
        mat_ok = res["items_mapped"]
        mat_miss = res["items_missing"]
        print(f"  [{i:03d}] {icon} {res['company_name_po'][:35]:<35} "
              f"PO={res['po_number']:<18} "
              f"Customer={'MATCH(' + res['sold_to_id'] + ')' if res['sold_to_id'] else 'NOT FOUND':<22} "
              f"Mat: {mat_ok}✅ {mat_miss}❌")

        if res["actions"]:
            for act in res["actions"]:
                print(f"         ⚠  {act[:90]}")

    # ── DB Reset ────────────────────────────────────────────────────────────
    _sec("Step 5/5 — Database Reset  (stuck emails → PENDING)")
    reset_actions = reset_stuck_emails(
        dry_run=args.dry_run,
        filter_status=args.status if not args.reset_all else None,
        filter_customer=args.customer,
    )
    for a in reset_actions:
        label = "(dry-run)" if a["dry_run"] else "→ PENDING"
        print(f"  🔄 {a['subject'][:50]:<50} [{a['old_status']}] {label}")

    # ── Summary ─────────────────────────────────────────────────────────────
    _sec("Summary")
    total_pos   = len(po_records)
    matched     = counts.get("MATCHED",   0)
    not_found   = counts.get("NOT_FOUND", 0)
    items_ok    = counts["items_ok"]
    items_miss  = counts["items_missing"]
    resets      = len(reset_actions)

    pct = (matched / total_pos * 100) if total_pos > 0 else 0.0
    print(f"""
  POs processed         : {total_pos:>6}
  Customer matched      : {matched:>6}  ({pct:.1f}% of POs)
  Customer NOT found    : {not_found:>6}
  ─────────────────────────────────
  Line items mapped     : {items_ok:>6}
  Line items MISSING    : {items_miss:>6}
  ─────────────────────────────────
  DB rows reset         : {resets:>6}  {'(dry-run — no actual changes)' if args.dry_run else '→ will be retried on next pipeline run'}
""")

    # ── Report ──────────────────────────────────────────────────────────────
    if not args.no_report:
        _sec("Writing Reports")
        write_report(resolutions, reset_actions, dry_run=args.dry_run)

    print("\n  ✅ Done.\n")


if __name__ == "__main__":
    main()
