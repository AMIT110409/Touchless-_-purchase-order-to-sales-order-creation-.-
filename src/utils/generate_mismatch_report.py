"""
=============================================================================
Missing / Mismatched Material Report Generator
=============================================================================
Reads ALL *_enriched.jsonl files from the current directory, collects every
line-item that has a missing internal_material_number, then cross-validates
each case against the Celonis Sheet 2 parquet cache to determine WHY it is
missing.

Output:
  missing_materials_report.xlsx   — multi-sheet Excel report

Celonis verdict logic:
  - "CUSTOMER NOT IN CELONIS"    -> customer_id not found in Sheet 2 at all
  - "MATERIAL NOT MAPPED"        -> customer exists but customer_material_number
                                    not found under any of their rows
  - "MAPPED - WRONG MATCH"       -> customer_material_number IS in Sheet 2 but
                                    extracted value does not match exactly
  - "MAPPED OK"                  -> internal_material_number resolved correctly
                                    (sanity-check row; should not appear here)

Usage:
  python generate_mismatch_report.py
  python generate_mismatch_report.py --folder "PO examples"
  python generate_mismatch_report.py --jsonl results_PO examples_enriched.jsonl
=============================================================================
"""

import argparse
import json
import os
import sys
import glob
from pathlib import Path
from datetime import datetime

os.environ["DISABLE_MODEL_SOURCE_CHECK"] = "True"
os.environ["PYTHONIOENCODING"] = "utf-8"

import pandas as pd
import openpyxl
from openpyxl.styles import (
    PatternFill, Font, Alignment, Border, Side, GradientFill
)
from openpyxl.utils import get_column_letter
from openpyxl.chart import BarChart, Reference

# ─────────────────────────── COLOUR PALETTE ──────────────────────────────────
HDR_FILL_DARK   = PatternFill("solid", fgColor="1F2D40")   # deep navy
HDR_FILL_BLUE   = PatternFill("solid", fgColor="2563EB")   # blue
HDR_FILL_AMBER  = PatternFill("solid", fgColor="D97706")   # amber
HDR_FILL_RED    = PatternFill("solid", fgColor="DC2626")   # red
HDR_FILL_GREEN  = PatternFill("solid", fgColor="059669")   # green
HDR_FILL_PURPLE = PatternFill("solid", fgColor="7C3AED")   # purple
HDR_FILL_GREY   = PatternFill("solid", fgColor="475569")   # slate

ROW_FILL_RED    = PatternFill("solid", fgColor="FEF2F2")
ROW_FILL_AMBER  = PatternFill("solid", fgColor="FFFBEB")
ROW_FILL_BLUE   = PatternFill("solid", fgColor="EFF6FF")
ROW_FILL_GREEN  = PatternFill("solid", fgColor="ECFDF5")

WHITE_FONT   = Font(color="FFFFFF", bold=True, size=10, name="Calibri")
BOLD_FONT    = Font(bold=True, size=10, name="Calibri")
NORMAL_FONT  = Font(size=9,  name="Calibri")
TITLE_FONT   = Font(bold=True, size=14, name="Calibri", color="1F2D40")
SUBTITLE_FONT= Font(size=10, name="Calibri", color="64748B")

CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
LEFT   = Alignment(horizontal="left",   vertical="center", wrap_text=True)
RIGHT  = Alignment(horizontal="right",  vertical="center")

def thin_border():
    s = Side(style="thin", color="CBD5E1")
    return Border(left=s, right=s, top=s, bottom=s)

def thick_bottom():
    t = Side(style="medium", color="94A3B8")
    s = Side(style="thin",   color="CBD5E1")
    return Border(left=s, right=s, top=s, bottom=t)

# ─────────────────────────── DATA LOADING ────────────────────────────────────

def load_celonis_sheet2(cache_dir=".celonis_cache"):
    parquet = os.path.join(cache_dir, "sheet2_customer_master.parquet")
    if not os.path.exists(parquet):
        print(f"  [WARN] Celonis cache not found at {parquet}")
        return None
    print(f"  Loading Celonis Sheet 2 ({parquet}) ...")
    df = pd.read_parquet(parquet)
    # normalise key columns
    for col in ["customer_id", "customer_material_number", "material_internal",
                "customer_name", "sales_organization"]:
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip()
    print(f"  Sheet 2 loaded: {len(df):,} rows, {df['customer_id'].nunique():,} unique customers")
    return df


def load_enriched_records(jsonl_files):
    records = []
    for fpath in jsonl_files:
        try:
            with open(fpath, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        except Exception as e:
            print(f"  [WARN] Cannot read {fpath}: {e}")
    print(f"  Loaded {len(records):,} enriched records from {len(jsonl_files)} file(s)")
    return records


# ─────────────────────────── MISMATCH DETECTION ──────────────────────────────

def celonis_verdict(customer_id, extracted_mat, celonis_df):
    """
    Returns (verdict_str, celonis_internal, celonis_cust_mat, celonis_description,
              celonis_sales_org, celonis_cust_name, all_customer_materials)
    """
    if celonis_df is None:
        return "CELONIS CACHE MISSING", "", "", "", "", "", ""

    cust_rows = celonis_df[celonis_df["customer_id"] == str(customer_id).strip()]
    if cust_rows.empty:
        return "CUSTOMER NOT IN CELONIS", "", "", "", "", "", ""

    cust_name     = cust_rows.iloc[0].get("customer_name", "")
    sales_org     = cust_rows.iloc[0].get("sales_organization", "")
    all_cust_mats = ", ".join(
        cust_rows["customer_material_number"].dropna().unique().tolist()[:10]
    )

    if not extracted_mat or str(extracted_mat).strip() in ("", "nan", "None"):
        return "NO MATERIAL EXTRACTED", "", "", "", sales_org, cust_name, all_cust_mats

    # exact match on customer_material_number
    mat_rows = cust_rows[
        cust_rows["customer_material_number"].str.strip().str.lower()
        == str(extracted_mat).strip().lower()
    ]
    if not mat_rows.empty:
        row = mat_rows.iloc[0]
        return (
            "MATERIAL FOUND - CHECK MAPPING",
            row.get("material_internal", ""),
            row.get("customer_material_number", ""),
            row.get("material_description", ""),
            sales_org, cust_name, all_cust_mats,
        )

    return "MATERIAL NOT IN CELONIS", "", "", "", sales_org, cust_name, all_cust_mats


def extract_missing_items(records, celonis_df):
    rows = []
    for rec in records:
        if not isinstance(rec, dict):
            continue

        source_file = rec.get("source_file", "")
        header      = rec.get("header_fields", {}) or {}
        sales_orders= rec.get("sales_orders",  []) or []

        # collect from sales_orders items (post-enrichment)
        for so in sales_orders:
            if not isinstance(so, dict):
                continue
            cust_id   = so.get("customer_number", "") or header.get("customer_number", "")
            cust_name = so.get("customer_name",   "") or header.get("customer_name_matched", "")
            sales_org = so.get("sales_organization", "")
            po_number = so.get("po_number", "")
            order_type= so.get("order_type", "")
            cg2       = so.get("customer_group2", "")
            so_label  = so.get("so_grouping_label", "")

            for item in (so.get("items") or []):
                if not isinstance(item, dict):
                    continue
                internal = item.get("internal_material_number", "") or ""
                # only missing ones
                if internal.strip().strip("0"):
                    continue   # has a real mapping → skip

                extracted_mat = (item.get("extracted_material_number") or
                                 item.get("customer_material_number")  or
                                 item.get("material_code", ""))
                mat_desc      = item.get("material_description", "")
                qty           = item.get("quantity", "")
                unit          = item.get("unit", "")
                delivery_date = item.get("delivery_date", "")

                (verdict, cel_internal, cel_cust_mat,
                 cel_desc, cel_sales_org, cel_cust_name,
                 all_mats) = celonis_verdict(cust_id, extracted_mat, celonis_df)

                rows.append({
                    "Source File"                  : os.path.basename(source_file),
                    "PO Number"                    : po_number,
                    "Customer ID"                  : cust_id,
                    "Customer Name"                : cust_name,
                    "Sales Organization"           : sales_org or cel_sales_org,
                    "Customer Group 2"             : cg2,
                    "SO Grouping Rule"             : so_label,
                    "Order Type"                   : order_type,
                    "Extracted Material #"         : extracted_mat,
                    "Material Description (PO)"    : mat_desc,
                    "Quantity"                     : qty,
                    "Unit"                         : unit,
                    "Delivery Date"                : delivery_date,
                    "Internal Material # (Resolved)": internal,
                    "Celonis Verdict"              : verdict,
                    "Celonis Internal Material"    : cel_internal,
                    "Celonis Cust Material"        : cel_cust_mat,
                    "Celonis Material Desc"        : cel_desc,
                    "All Celonis Mats for Customer": all_mats,
                    "Action Required"              : _action(verdict),
                })

        # fallback: if no sales_orders, check raw line_items
        if not sales_orders:
            cust_id   = header.get("customer_number", "") or header.get("customer_id_or_name", "")
            cust_name = header.get("customer_name_matched", "") or header.get("customer_name", "")
            po_number = header.get("po_number", "")
            sales_org = header.get("sales_organization", "")

            for item in (rec.get("line_items") or []):
                if not isinstance(item, dict):
                    continue
                internal = item.get("internal_material", "") or item.get("internal_material_number", "") or ""
                if internal.strip().strip("0"):
                    continue
                extracted_mat = item.get("material_code", "") or item.get("extracted_material_number", "")
                mat_desc      = item.get("material_description", "") or item.get("description", "")
                qty           = item.get("quantity", "")
                unit          = item.get("unit", "")
                delivery_date = item.get("delivery_date", "")

                (verdict, cel_internal, cel_cust_mat,
                 cel_desc, cel_sales_org, cel_cust_name,
                 all_mats) = celonis_verdict(cust_id, extracted_mat, celonis_df)

                rows.append({
                    "Source File"                  : os.path.basename(source_file),
                    "PO Number"                    : po_number,
                    "Customer ID"                  : cust_id,
                    "Customer Name"                : cust_name,
                    "Sales Organization"           : sales_org or cel_sales_org,
                    "Customer Group 2"             : "",
                    "SO Grouping Rule"             : "",
                    "Order Type"                   : "",
                    "Extracted Material #"         : extracted_mat,
                    "Material Description (PO)"    : mat_desc,
                    "Quantity"                     : qty,
                    "Unit"                         : unit,
                    "Delivery Date"                : delivery_date,
                    "Internal Material # (Resolved)": internal,
                    "Celonis Verdict"              : verdict,
                    "Celonis Internal Material"    : cel_internal,
                    "Celonis Cust Material"        : cel_cust_mat,
                    "Celonis Material Desc"        : cel_desc,
                    "All Celonis Mats for Customer": all_mats,
                    "Action Required"              : _action(verdict),
                })

    return rows


def _action(verdict):
    if verdict == "CUSTOMER NOT IN CELONIS":
        return "ADD CUSTOMER TO CELONIS MASTER DATA"
    if verdict == "MATERIAL NOT IN CELONIS":
        return "ADD MATERIAL MAPPING TO CELONIS SHEET 2"
    if verdict == "MATERIAL FOUND - CHECK MAPPING":
        return "REVIEW MAPPING — FOUND IN CELONIS BUT NOT RESOLVED"
    if verdict == "NO MATERIAL EXTRACTED":
        return "RE-CHECK OCR / EXTRACTION LOGIC"
    if verdict == "CELONIS CACHE MISSING":
        return "REFRESH CELONIS CACHE (run celonis_to_azure.py)"
    return "INVESTIGATE"


# ─────────────────────────── EXCEL WRITER ────────────────────────────────────

def style_header_row(ws, row, col_count, fill, font=None):
    font = font or WHITE_FONT
    for c in range(1, col_count + 1):
        cell = ws.cell(row=row, column=c)
        cell.fill   = fill
        cell.font   = font
        cell.alignment = CENTER
        cell.border = thick_bottom()


def auto_col_width(ws, min_w=10, max_w=50):
    for col in ws.columns:
        max_len = max_w
        col_letter = get_column_letter(col[0].column)
        try:
            max_len = min(max_w, max(
                max(len(str(cell.value)) if cell.value else 0 for cell in col) + 2,
                min_w
            ))
        except Exception:
            pass
        ws.column_dimensions[col_letter].width = max_len


def verdict_fill(verdict):
    if "NOT IN CELONIS" in verdict or "NOT MAPPED" in verdict:
        return ROW_FILL_RED
    if "FOUND - CHECK" in verdict:
        return ROW_FILL_AMBER
    if "NO MATERIAL" in verdict:
        return ROW_FILL_AMBER
    if "CACHE MISSING" in verdict:
        return ROW_FILL_BLUE
    return ROW_FILL_GREEN


def write_detail_sheet(wb, df, sheet_name, header_fill):
    ws = wb.create_sheet(title=sheet_name[:31])
    cols = list(df.columns)

    # Header row
    for ci, col in enumerate(cols, 1):
        cell = ws.cell(row=1, column=ci, value=col)
        cell.fill      = header_fill
        cell.font      = WHITE_FONT
        cell.alignment = CENTER
        cell.border    = thick_bottom()

    # Data rows
    for ri, (_, row) in enumerate(df.iterrows(), 2):
        verdict = str(row.get("Celonis Verdict", ""))
        rfill   = verdict_fill(verdict)
        for ci, col in enumerate(cols, 1):
            val  = row[col]
            cell = ws.cell(row=ri, column=ci, value=val)
            cell.fill      = rfill
            cell.font      = NORMAL_FONT
            cell.alignment = LEFT
            cell.border    = thin_border()

    # Freeze header
    ws.freeze_panes = "A2"
    auto_col_width(ws)
    ws.row_dimensions[1].height = 30
    return ws


def write_summary_sheet(wb, df):
    ws = wb.create_sheet(title="SUMMARY", index=0)
    ws.sheet_view.showGridLines = False

    # Title block
    ws.merge_cells("A1:H1")
    tc = ws["A1"]
    tc.value     = "Missing / Mismatched Material Report"
    tc.font      = TITLE_FONT
    tc.alignment = CENTER
    tc.fill      = PatternFill("solid", fgColor="1F2D40")
    tc.font      = Font(bold=True, size=16, color="FFFFFF", name="Calibri")
    ws.row_dimensions[1].height = 36

    ws.merge_cells("A2:H2")
    sc = ws["A2"]
    sc.value     = f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}   |   Total Missing Items: {len(df)}"
    sc.font      = Font(size=10, color="64748B", name="Calibri", italic=True)
    sc.alignment = CENTER
    sc.fill      = PatternFill("solid", fgColor="F8FAFC")
    ws.row_dimensions[2].height = 22

    # Verdict breakdown table
    verdict_counts = df["Celonis Verdict"].value_counts().reset_index()
    verdict_counts.columns = ["Celonis Verdict", "Count"]

    ws.row_dimensions[4].height = 26
    headers_s = ["Celonis Verdict", "Count", "% of Total", "Action Required"]
    fills_s   = [HDR_FILL_DARK, HDR_FILL_DARK, HDR_FILL_DARK, HDR_FILL_DARK]
    for ci, h in enumerate(headers_s, 1):
        cell = ws.cell(row=4, column=ci, value=h)
        cell.fill      = HDR_FILL_DARK
        cell.font      = WHITE_FONT
        cell.alignment = CENTER
        cell.border    = thick_bottom()

    total = len(df)
    for ri, (_, vrow) in enumerate(verdict_counts.iterrows(), 5):
        verdict = vrow["Celonis Verdict"]
        count   = vrow["Count"]
        pct     = f"{count/total*100:.1f}%" if total else "0%"
        action  = _action(verdict)
        rfill   = verdict_fill(verdict)
        for ci, val in enumerate([verdict, count, pct, action], 1):
            cell = ws.cell(row=ri, column=ci, value=val)
            cell.fill      = rfill
            cell.font      = NORMAL_FONT if ci > 1 else Font(bold=True, size=9, name="Calibri")
            cell.alignment = CENTER if ci > 1 else LEFT
            cell.border    = thin_border()

    end_row = 5 + len(verdict_counts)

    # Customer breakdown
    ws.row_dimensions[end_row + 1].height = 10
    cust_row = end_row + 2
    ws.merge_cells(f"A{cust_row}:H{cust_row}")
    ch = ws[f"A{cust_row}"]
    ch.value     = "Missing Items by Customer"
    ch.font      = Font(bold=True, size=12, color="FFFFFF", name="Calibri")
    ch.alignment = CENTER
    ch.fill      = HDR_FILL_BLUE
    ws.row_dimensions[cust_row].height = 26

    cust_counts = (
        df.groupby(["Customer ID", "Customer Name"])
          .size()
          .reset_index(name="Missing Items")
          .sort_values("Missing Items", ascending=False)
    )
    cust_headers = ["Customer ID", "Customer Name", "Missing Items", "Sales Org", "Unique Materials"]
    cust_row_h   = cust_row + 1
    for ci, h in enumerate(cust_headers, 1):
        cell = ws.cell(row=cust_row_h, column=ci, value=h)
        cell.fill = HDR_FILL_BLUE; cell.font = WHITE_FONT
        cell.alignment = CENTER;   cell.border = thick_bottom()

    for ri, (_, crow) in enumerate(cust_counts.iterrows(), cust_row_h + 1):
        cid  = crow["Customer ID"]
        cname= crow["Customer Name"]
        cnt  = crow["Missing Items"]
        sorg = df[df["Customer ID"] == cid]["Sales Organization"].mode()
        sorg = sorg.iloc[0] if not sorg.empty else ""
        umats= df[df["Customer ID"] == cid]["Extracted Material #"].nunique()
        alt  = (ri % 2 == 0)
        rfill= PatternFill("solid", fgColor="EFF6FF") if alt else PatternFill("solid", fgColor="FFFFFF")
        for ci, val in enumerate([cid, cname, cnt, sorg, umats], 1):
            cell = ws.cell(row=ri, column=ci, value=val)
            cell.fill = rfill; cell.font = NORMAL_FONT
            cell.alignment = CENTER if ci > 2 else LEFT
            cell.border = thin_border()

    auto_col_width(ws, min_w=12, max_w=60)
    ws.column_dimensions["A"].width = 20
    ws.column_dimensions["B"].width = 32
    ws.column_dimensions["C"].width = 14
    ws.column_dimensions["D"].width = 44
    return ws


def write_celonis_lookup_sheet(wb, df, celonis_df):
    """Sheet showing what IS in Celonis for each affected customer"""
    ws = wb.create_sheet(title="Celonis Customer Data")
    if celonis_df is None:
        ws["A1"] = "Celonis cache not available."
        return ws

    # Get unique customers from mismatch report
    affected_ids = df["Customer ID"].dropna().unique().tolist()
    cel_subset   = celonis_df[celonis_df["customer_id"].isin(affected_ids)].copy()

    display_cols = ["customer_id", "customer_name", "sales_organization",
                    "customer_material_number", "material_internal",
                    "material_description", "customer_group2"]
    display_cols = [c for c in display_cols if c in cel_subset.columns]
    cel_subset   = cel_subset[display_cols].drop_duplicates().reset_index(drop=True)

    nice_names = {
        "customer_id"              : "Customer ID",
        "customer_name"            : "Customer Name",
        "sales_organization"       : "Sales Org",
        "customer_material_number" : "Customer Material #",
        "material_internal"        : "Internal Material #",
        "material_description"     : "Material Description",
        "customer_group2"          : "Customer Group 2",
    }
    cel_subset.rename(columns=nice_names, inplace=True)
    cols = list(cel_subset.columns)

    # Header
    for ci, col in enumerate(cols, 1):
        cell = ws.cell(row=1, column=ci, value=col)
        cell.fill = HDR_FILL_GREEN; cell.font = WHITE_FONT
        cell.alignment = CENTER;    cell.border = thick_bottom()

    # Data
    for ri, (_, row) in enumerate(cel_subset.iterrows(), 2):
        alt   = (ri % 2 == 0)
        rfill = PatternFill("solid", fgColor="ECFDF5") if alt else PatternFill("solid", fgColor="FFFFFF")
        for ci, col in enumerate(cols, 1):
            cell = ws.cell(row=ri, column=ci, value=row[col])
            cell.fill = rfill; cell.font = NORMAL_FONT
            cell.alignment = LEFT; cell.border = thin_border()

    ws.freeze_panes = "A2"
    auto_col_width(ws, min_w=14, max_w=50)
    return ws


# ─────────────────────────── MAIN ────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Generate Missing Material Mismatch Report")
    parser.add_argument("--jsonl",   type=str, default=None,
                        help="Specific enriched JSONL file to analyse (default: all *_enriched.jsonl)")
    parser.add_argument("--folder",  type=str, default=".",
                        help="Folder to scan for enriched JSONL files")
    parser.add_argument("--output",  type=str, default="missing_materials_report.xlsx",
                        help="Output Excel file path")
    parser.add_argument("--cache",   type=str, default=".celonis_cache",
                        help="Celonis parquet cache directory")
    args = parser.parse_args()

    print("\n" + "="*65)
    print("  MISSING MATERIAL MISMATCH REPORT GENERATOR")
    print("="*65)

    # 1. Find JSONL files
    if args.jsonl:
        jsonl_files = [args.jsonl]
    else:
        base = Path(args.folder)
        jsonl_files = sorted(base.glob("*_enriched.jsonl"))
        if not jsonl_files:
            jsonl_files = sorted(base.glob("results_*.jsonl"))
        if not jsonl_files:
            print(f"  [ERROR] No *_enriched.jsonl files found in '{args.folder}'")
            sys.exit(1)

    print(f"\n  JSONL files to process ({len(jsonl_files)}):")
    for f in jsonl_files:
        print(f"    - {f}")

    # 2. Load Celonis cache
    print("\n  Loading Celonis cache ...")
    celonis_df = load_celonis_sheet2(args.cache)

    # 3. Load enriched records
    print("\n  Loading enriched pipeline records ...")
    records = load_enriched_records([str(f) for f in jsonl_files])

    # 4. Extract mismatch rows
    print("\n  Extracting missing/mismatched material cases ...")
    rows = extract_missing_items(records, celonis_df)

    if not rows:
        print("\n  [OK] No missing material cases found! All materials are mapped.")
        return

    df = pd.DataFrame(rows)
    df = df.drop_duplicates(
        subset=["Customer ID", "Extracted Material #", "PO Number", "Source File"]
    ).reset_index(drop=True)

    print(f"\n  Found {len(df)} unique missing-material line items")
    print(f"  Verdicts breakdown:")
    for v, c in df["Celonis Verdict"].value_counts().items():
        print(f"    {c:4d}  {v}")

    # 5. Write Excel
    print(f"\n  Writing Excel report -> {args.output} ...")
    wb = openpyxl.Workbook()
    wb.remove(wb.active)  # remove default blank sheet

    # Sheet 1: Summary dashboard
    write_summary_sheet(wb, df)

    # Sheet 2: All missing items (full detail)
    write_detail_sheet(wb, df, "All Missing Items", HDR_FILL_RED)

    # Sheet 3-N: Split by verdict
    for verdict, group in df.groupby("Celonis Verdict"):
        short = verdict.replace("CELONIS", "CEL").replace(" - ", "-")[:28]
        fills = {
            "CUSTOMER NOT IN CELONIS" : HDR_FILL_RED,
            "MATERIAL NOT IN CELONIS" : HDR_FILL_AMBER,
            "MATERIAL FOUND - CHECK MAPPING": HDR_FILL_PURPLE,
            "NO MATERIAL EXTRACTED"   : HDR_FILL_GREY,
        }
        fill = fills.get(verdict, HDR_FILL_DARK)
        write_detail_sheet(wb, group.reset_index(drop=True), short, fill)

    # Sheet: Celonis data for affected customers
    write_celonis_lookup_sheet(wb, df, celonis_df)

    wb.save(args.output)
    print(f"\n  [DONE] Report saved: {os.path.abspath(args.output)}")
    print(f"         Sheets: {[s.title for s in wb.worksheets]}")
    print("="*65 + "\n")


if __name__ == "__main__":
    main()
