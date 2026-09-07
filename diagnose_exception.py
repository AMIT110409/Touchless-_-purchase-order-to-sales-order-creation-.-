#!/usr/bin/env python
"""
diagnose_exception.py
=====================
Quick exception diagnostic tool for the Touchless Order pipeline.

Usage:
    python diagnose_exception.py <path_to_pdf_or_jsonl>
    python diagnose_exception.py --customer "EVCO PLASTICS"
    python diagnose_exception.py --po-number 302323-AMP
    python diagnose_exception.py --customer-id 4020012056
    python diagnose_exception.py --material 1481272
    python diagnose_exception.py --list-exceptions
    python diagnose_exception.py --refresh-cache
"""

import sys
import os
import json
import argparse
import subprocess
from pathlib import Path

# ─────────────────────────────────────────────────────────
# Setup paths
# ─────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ─────────────────────────────────────────────────────────
# Data helpers
# ─────────────────────────────────────────────────────────

def _load_master():
    import pandas as pd
    cache = SCRIPT_DIR / ".celonis_cache" / "test_mp_customer_master.parquet"
    if not cache.exists():
        print("[WARN] Customer master cache not found. Run: python celonis_to_azure.py")
        return pd.DataFrame()
    return pd.read_parquet(str(cache))


def _load_order_mapping():
    import pandas as pd
    cache = SCRIPT_DIR / ".celonis_cache" / "sheet3_order_mapping.parquet"
    if not cache.exists():
        return pd.DataFrame()
    return pd.read_parquet(str(cache))


def _sec(title):
    print()
    print("=" * 62)
    print(f"  {title}")
    print("=" * 62)


# ─────────────────────────────────────────────────────────
# Lookup commands
# ─────────────────────────────────────────────────────────

def lookup_customer(query: str):
    df = _load_master()
    if df.empty:
        return
    q = query.strip().upper()
    mask = (
        df["sold_to_id"].str.strip() == q
    ) | (
        df["sold_to_name_full"].str.upper().str.contains(q, na=False)
    ) | (
        df["ship_to_name_full"].str.upper().str.contains(q, na=False)
    )
    sub = df[mask]
    _sec(f"Customer Lookup: '{query}'")
    print(f"  Total matching rows: {len(sub)}")
    if sub.empty:
        print("  [NO MATCH] Not found in Celonis customer master.")
        return

    sold_tos = sub[["sold_to_id", "sold_to_name_full", "sold_to_postcode"]].drop_duplicates()
    print(f"\n  --- Sold-To Accounts ({len(sold_tos)}) ---")
    for _, r in sold_tos.iterrows():
        print(f"    {r['sold_to_id']}  {str(r['sold_to_name_full'])[:55]}  PC={r['sold_to_postcode']}")

    ship_tos = sub[["sold_to_id", "ship_to_id", "ship_to_name_full", "ship_to_postcode"]].drop_duplicates()
    print(f"\n  --- Ship-To Nodes ({len(ship_tos)}) ---")
    for _, r in ship_tos.iterrows():
        print(f"    Sold={r['sold_to_id']}  Ship={r['ship_to_id']}  {str(r['ship_to_name_full'])[:40]}  PC={r['ship_to_postcode']}")

    mats = sub[["sold_to_id", "customer_material_number", "material_description", "material_internal"]].drop_duplicates()
    print(f"\n  --- Customer Materials ({len(mats)}) ---")
    for _, r in mats.head(40).iterrows():
        print(f"    Sold={r['sold_to_id']}  CustMat={r['customer_material_number']}  "
              f"{str(r['material_description'])[:40]}  Int={r['material_internal']}")
    if len(mats) > 40:
        print(f"    ... and {len(mats) - 40} more rows")


def lookup_material(mat_code: str):
    df = _load_master()
    om = _load_order_mapping()
    if df.empty:
        return
    q = mat_code.strip().upper()
    mask = (
        df["customer_material_number"].astype(str).str.strip().str.upper() == q
    ) | (
        df["material_internal"].str.strip() == q
    ) | (
        df["material_description"].str.upper().str.contains(q, na=False)
    )
    sub = df[mask]
    _sec(f"Material Lookup: '{mat_code}'")
    print(f"  Matching rows: {len(sub)}")
    if sub.empty:
        print("  [NO MATCH] Material not found in customer master.")
        return
    uniq = sub[["sold_to_id", "sold_to_name_full", "customer_material_number",
                "material_description", "material_internal"]].drop_duplicates()
    for _, r in uniq.iterrows():
        print(f"    Sold={r['sold_to_id']}  CustMat={r['customer_material_number']}  "
              f"{str(r['material_description'])[:45]}  Int={r['material_internal']}")

    if not om.empty:
        internal_ids = sub["material_internal"].dropna().unique().tolist()
        hist = om[om["material_internal"].isin(internal_ids)]
        print(f"\n  Historical SOs for these materials: {len(hist)}")
        for _, r in hist.head(20).iterrows():
            print(f"    Cust={r['customer_id']}  Mat={r['material_internal']}  SO={r['sales_order']}  PO={r.get('customer_po_number', '')}")


def lookup_po_number(po_number: str):
    om = _load_order_mapping()
    if om.empty:
        return
    _sec(f"PO Number Lookup: '{po_number}'")
    q = po_number.strip().upper()
    sub = om[om["customer_po_number"].astype(str).str.upper().str.contains(q, na=False)]
    print(f"  Matching historical orders: {len(sub)}")
    if sub.empty:
        print("  [NOT FOUND] No historical SO for this PO number.")
    else:
        for _, r in sub.iterrows():
            print(f"    Cust={r['customer_id']}  Mat={r['material_internal']}  "
                  f"SO={r['sales_order']}  Type={r['order_type']}  Date={r.get('creation_date', '')}")


# ─────────────────────────────────────────────────────────
# Full pipeline diagnostic
# ─────────────────────────────────────────────────────────

def _diagnose_record(rec: dict):
    hdr = rec.get("header_fields", {})
    sales_orders = rec.get("sales_orders", [])

    # Gather fields from whichever structure is populated
    so = sales_orders[0] if sales_orders else {}
    sold_to_id = so.get("sold_to_id") or hdr.get("sold_to_id") or hdr.get("customer_number", "")
    sold_to_name = so.get("customer_name") or hdr.get("sold_to_name", "")
    ship_to_id = so.get("ship_to_id") or hdr.get("ship_to_id", "")
    ship_to_name = so.get("ship_to_name") or hdr.get("ship_to_name", "")
    conf_raw = hdr.get("customer_confidence", rec.get("confidence_score", None))
    conf_str = f"{float(conf_raw)*100:.0f}%" if isinstance(conf_raw, (int, float)) else str(conf_raw or "?")
    warnings = rec.get("warnings", so.get("warnings", []))
    exc_reason = rec.get("exception_reason", "")

    # Derive status
    if sales_orders and so.get("items") and all(i.get("internal_material_number") for i in so["items"]):
        status = "MATCHED ✅"
    elif not sold_to_id:
        status = "NO CUSTOMER MATCH ❌"
    else:
        status = "PARTIAL / EXCEPTION ⚠"

    print(f"  PO Number  : {hdr.get('po_number', so.get('po_number', '?'))}")
    print(f"  Customer   : {hdr.get('customer_name', hdr.get('customer_id_or_name', '?'))}")
    print(f"  Sold-To    : {sold_to_id}  {sold_to_name}")
    print(f"  Ship-To    : {ship_to_id}  {ship_to_name}")
    print(f"  Sales Org  : {so.get('sales_organization', '?')}")
    print(f"  Order Type : {so.get('order_type', '?')}")
    print(f"  Status     : {status}")
    print(f"  Confidence : {conf_str}")
    print(f"  Explanation: {hdr.get('customer_confidence_explanation', '')}")

    if warnings:
        print(f"\n  Warnings:")
        for w in warnings:
            print(f"    ⚠  {w}")

    if exc_reason:
        print(f"\n  Exception reason: {exc_reason}")

    # Items from sales_orders.items (enriched)
    items = so.get("items", [])
    if items:
        print("\n  Mapped Line Items (from sales_orders):")
        for it in items:
            internal = it.get("internal_material_number", "[NOT MAPPED]")
            cust_mat = it.get("extracted_material_number") or it.get("customer_material_number", "?")
            desc = str(it.get("material_description", ""))[:50]
            otype = it.get("order_type", "?")
            print(f"    CustMat={cust_mat}  -> Internal={internal}")
            print(f"             Desc={desc}  OType={otype}")
    else:
        # Fallback: raw line items
        print("\n  Raw Line Items (not yet enriched):")
        for li in rec.get("line_items", []):
            print(f"    Line {li.get('line_number', '?')}: CustMat={li.get('material_code', '?')}  "
                  f"Desc={str(li.get('material_description', ''))[:45]}")
            print(f"             -> Internal=[NOT MAPPED]")

    print("\n  --- ROOT CAUSE ANALYSIS ---")
    if "MATCHED" in status:
        print("  ✅ PO is fully matched — all materials have internal codes.")
        print("     If Celonis still shows as exception, it may be a dashboard cache/view issue.")
    else:
        reasons = []
        if not sold_to_id:
            reasons.append("Sold-to ID could not be matched from customer name/postcode")
        if not ship_to_id:
            reasons.append("Ship-to ID could not be matched")
        unmapped_items = [i for i in items if not i.get("internal_material_number")]
        if not items:
            unmapped_items = rec.get("line_items", [])
        if unmapped_items:
            reasons.append(f"{len(unmapped_items)} material(s) have no internal mapping")
        if isinstance(conf_raw, (int, float)) and conf_raw < 0.70:
            reasons.append(f"Confidence too low: {float(conf_raw)*100:.0f}% (threshold: 70%)")
        if not reasons:
            reasons.append("See warnings above for details")
        for r in reasons:
            print(f"  ❌ {r}")


def run_full_diagnostic(input_path: str):
    _sec(f"Full Pipeline Diagnostic: {Path(input_path).name}")
    path = Path(input_path).resolve()
    if not path.exists():
        print(f"  [ERROR] File not found: {input_path}")
        return

    if path.suffix.lower() == ".pdf":
        # Try to find a pre-existing raw JSONL first
        raw_stem = f"test_raw_{path.stem}.jsonl"
        raw_candidate = SCRIPT_DIR / raw_stem
        if raw_candidate.exists():
            print(f"  Found pre-extracted JSONL: {raw_stem} — using it directly.")
            print("  (To re-extract, delete that file first.)")
            raw_jsonl = str(raw_candidate)
        else:
            print(f"  [WARN] No pre-extracted JSONL found for this PDF.")
            print(f"         Expected: {raw_stem}")
            print(f"         Run Stage 1 OCR extraction manually, then pass the .jsonl file to this script.")
            print(f"         Example: python run_outlook_to_pipeline.py --stage 1 --folder <dir>")
            return
    elif path.suffix.lower() == ".jsonl":
        raw_jsonl = str(path)
        print(f"  Using JSONL: {raw_jsonl}")
    else:
        print(f"  [ERROR] Unsupported file type: {path.suffix}  (use .pdf or .jsonl)")
        return

    print("\n  Running re-enrichment (Stage 2)...")
    from reenrich_results import reenrich
    out_path = str(path.with_suffix("")) + "_diag_enriched.jsonl"
    reenrich(raw_jsonl, out_path)

    if not os.path.exists(out_path):
        print("  [ERROR] Re-enrichment produced no output.")
        return

    records = []
    with open(out_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    print(f"\n  {len(records)} enriched record(s) found.")
    for i, rec in enumerate(records):
        print(f"\n{'─' * 62}")
        print(f"  Record {i+1}/{len(records)}")
        _diagnose_record(rec)


# ─────────────────────────────────────────────────────────
# List SO creation results
# ─────────────────────────────────────────────────────────

def list_exceptions():
    import pandas as pd
    cache = SCRIPT_DIR / ".celonis_cache" / "so_creation_results.parquet"
    if not cache.exists():
        print("[WARN] so_creation_results.parquet not found. Run: python celonis_to_azure.py")
        return
    df = pd.read_parquet(str(cache))
    _sec(f"SO Creation Results  ({len(df)} total rows)")

    failed = df[df["STATUS"].str.upper() != "SUCCESS"]
    print(f"\n  ❌ Failed / Exceptions: {len(failed)}")
    if not failed.empty:
        cols = [c for c in ["PO_NUMBER", "STATUS", "BLOCK_CODE", "BLOCK_REASON",
                             "FAILURE_REASON", "CUSTOMER_NAME"] if c in failed.columns]
        print(failed[cols].to_string(index=False))

    ok = df[df["STATUS"].str.upper() == "SUCCESS"]
    print(f"\n  ✅ Successful SOs: {len(ok)}")
    if not ok.empty:
        cols = [c for c in ["PO_NUMBER", "SO_NUMBER", "CUSTOMER_NAME", "SALES_ORG"] if c in ok.columns]
        print(ok[cols].to_string(index=False))


# ─────────────────────────────────────────────────────────
# Refresh cache
# ─────────────────────────────────────────────────────────

def refresh_cache():
    _sec("Refreshing Celonis Cache")
    print("  Running celonis_to_azure.py ...")
    result = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "celonis_to_azure.py")],
        cwd=str(SCRIPT_DIR),
    )
    if result.returncode == 0:
        print("  ✅ Cache refreshed.")
    else:
        print("  ⚠  Cache refresh had errors — continuing with existing cache.")


# ─────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Exception Diagnostic Tool — Touchless Order Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python diagnose_exception.py test_raw_evco.jsonl
  python diagnose_exception.py 302323-AMP.pdf
  python diagnose_exception.py --customer "EVCO PLASTICS"
  python diagnose_exception.py --customer-id 4020012056
  python diagnose_exception.py --material 1481272
  python diagnose_exception.py --po-number 302323-AMP
  python diagnose_exception.py --list-exceptions
  python diagnose_exception.py --refresh-cache
""",
    )
    parser.add_argument("file", nargs="?", help="PDF or raw JSONL file to diagnose")
    parser.add_argument("--customer", "-c", metavar="NAME", help="Customer name keyword search")
    parser.add_argument("--customer-id", metavar="ID", help="Exact sold-to customer ID")
    parser.add_argument("--material", "-m", metavar="CODE", help="Customer material number or description keyword")
    parser.add_argument("--po-number", "-p", metavar="PO", help="PO number to look up in order history")
    parser.add_argument("--list-exceptions", "-l", action="store_true",
                        help="List all SO exceptions from the Celonis view")
    parser.add_argument("--refresh-cache", "-r", action="store_true",
                        help="Re-pull all Celonis tables before running")

    args = parser.parse_args()

    if args.refresh_cache:
        refresh_cache()

    ran = False
    if args.file:
        run_full_diagnostic(args.file)
        ran = True
    if args.customer:
        lookup_customer(args.customer)
        ran = True
    if args.customer_id:
        lookup_customer(args.customer_id)
        ran = True
    if args.material:
        lookup_material(args.material)
        ran = True
    if args.po_number:
        lookup_po_number(args.po_number)
        ran = True
    if args.list_exceptions:
        list_exceptions()
        ran = True

    if not ran:
        parser.print_help()


if __name__ == "__main__":
    main()
