"""
Diagnose extraction and mapping for a single customer/file.
Use to find root causes before fixing.

Usage:
  python diagnose_single_customer.py --file 4504063626.pdf
  python diagnose_single_customer.py --source 4504063626  (search in results_po_full_enriched.jsonl)
"""
import argparse
import json
import os
os.environ['DISABLE_MODEL_SOURCE_CHECK'] = 'True'

import re

def load_record(
    source: str,
    jsonl_path: str = "results_po_full_enriched.jsonl",
):
    """Load a record by source_file or customer number."""
    if not os.path.exists(jsonl_path):
        print(f"File not found: {jsonl_path}")
        return None
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            src = rec.get("source_file", "")
            h = rec.get("header_fields", {})
            cid = h.get("customer_id_or_name", "")
            cnum = h.get("customer_number", "")
            cname = h.get("customer_name_matched", "")
            if (
                source in src
                or source == cid
                or source == cnum
                or (source and source.upper() in (cname or "").upper())
            ):
                return rec
    return None

def is_weight_like(s: str) -> bool:
    """Check if string looks like a weight/quantity, not a material code."""
    if not s or not isinstance(s, str):
        return False
    s = s.strip()
    # Pattern: "25Kg", "500 KG", "1.500 kg", "1000 pcs"
    if re.match(r"^\d+([.,]\d+)?\s*(kg|KG|pcs|pc|ton|lb|g)\s*$", s, re.IGNORECASE):
        return True
    if re.match(r"^\d+([.,]\d+)?\s*(kg|KG|pcs|ton)\s*$", s, re.IGNORECASE):
        return True
    return False

def diagnose(rec: dict):
    if not rec:
        print("No record found.")
        return

    header = rec.get("header_fields", {})
    items = rec.get("line_items", [])
    sos = rec.get("sales_orders", [])

    print("\n" + "=" * 70)
    print(f"SOURCE FILE: {rec.get('source_file', '?')}")
    print("=" * 70)

    print("\n--- HEADER ---")
    print(f"  customer_id_or_name: {header.get('customer_id_or_name')}")
    print(f"  customer_number:     {header.get('customer_number')} (used for Excel lookup)")
    print(f"  vendor_id:           {header.get('vendor_id')}")
    print(f"  sold_to_id:          {header.get('sold_to_id')}")
    print(f"  ship_to_id:          {header.get('ship_to_id')}")

    print("\n--- LINE ITEMS (material code check) ---")
    for i, item in enumerate(items):
        mc = item.get("material_code", "")
        desc = item.get("material_description", "")
        internal = item.get("internal_material_number", "")
        cust_mat = item.get("customer_material_number", "")
        w = "[WEIGHT?]" if is_weight_like(mc) else ""
        print(f"  [{i+1}] material_code: '{mc}' {w}")
        print(f"       material_description: '{desc}'")
        print(f"       internal_material_number: '{internal}' | customer_material_number: '{cust_mat}'")

    print("\n--- SALES ORDER MAPPING ---")
    if sos:
        for so in sos[:1]:  # First SO
            print(f"  sales_org: {so.get('sales_organization')} | order_type: {so.get('order_type')}")
            for it in so.get("items", [])[:2]:
                print(f"    -> internal: {it.get('internal_material_number')} | customer_mat: {it.get('customer_material_number')}")
    else:
        print("  (No sales orders - mapping failed)")

    print("\n--- DIAGNOSIS ---")
    issues = []
    cust_num = header.get("customer_number")
    if not cust_num and header.get("customer_id_or_name"):
        issues.append("customer_number is EMPTY - Excel mapper falls back to vendor_id (wrong customer)")
    for item in items:
        mc = item.get("material_code", "")
        if is_weight_like(mc):
            issues.append(f"material_code '{mc}' looks like WEIGHT, not material code -> Excel lookup fails")
        if not item.get("internal_material_number") and not item.get("customer_material_number"):
            issues.append("No internal/customer material from Excel - mapping failed for this item")

    if issues:
        for i, iss in enumerate(issues, 1):
            print(f"  {i}. {iss}")
    else:
        print("  No obvious issues found.")

    print()

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--source", type=str, default="4504063626", help="Source file name or customer ID to search")
    p.add_argument("--input", type=str, default="results_po_full_enriched.jsonl")
    args = p.parse_args()
    rec = load_record(args.source, args.input)
    diagnose(rec)
