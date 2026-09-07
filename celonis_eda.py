"""
celonis_eda.py
==============
Exploratory Data Analysis (EDA) of Celonis master data.
Reads local .parquet cache files and produces a data-quality report for the client.

Checks (per dataset):
  Test MP  : missing customer_material_number, customer_material_description,
             sold_to_postcode, ship_to_postcode, sold_to_name_full, ship_to_name_full,
             salesperson_name (CSR), salesperson_email, material_description
  Sheet 2  : missing customer_material_number, customer_material_description,
             salesperson_email, sold_to/ship_to, material_description
  Sheet 3  : customer/material/SO coverage
  SO Results: STATUS breakdown (SUCCESS / FAILED / BLOCKED)

Output:
  - Console report (always)
  - celonis_eda_report.xlsx  (Excel workbook with one sheet per dataset)

Usage:
    python celonis_eda.py
    python celonis_eda.py --no-excel   # console only
"""

import os
import sys
import argparse
from pathlib import Path
from datetime import datetime

import pandas as pd

# ─── Config ───────────────────────────────────────────────────────────────────
CACHE_DIR   = Path(__file__).parent / ".celonis_cache"
OUTPUT_XLS  = Path(__file__).parent / "celonis_eda_report.xlsx"

EMPTY_VALS  = {"", "nan", "none", "null", "n/a", "na", "-", "#n/a"}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def is_empty(series: pd.Series) -> pd.Series:
    """Return boolean mask where value is blank / null / empty-string."""
    return series.isna() | series.astype(str).str.strip().str.lower().isin(EMPTY_VALS)


def pct(count, total):
    if total == 0:
        return "N/A"
    return f"{count / total * 100:.1f}%"


def banner(title: str):
    print("\n" + "=" * 72)
    print(f"  {title}")
    print("=" * 72)


def sub(title: str):
    print(f"\n  -- {title} --")


def row_stat(label, count, total, indent=4):
    spaces = " " * indent
    bar_len = 30
    filled  = int(bar_len * count / total) if total else 0
    bar     = "#" * filled + "." * (bar_len - filled)
    print(f"{spaces}{label:<42} {count:>8,}  /  {total:>8,}  ({pct(count, total)})  [{bar}]")


def load(name: str):
    path = CACHE_DIR / name
    if not path.exists():
        print(f"  [WARN] Not found: {path}")
        return None
    df = pd.read_parquet(path)
    # Normalize all string columns
    for col in df.select_dtypes(include="object").columns:
        df[col] = df[col].fillna("").astype(str).str.strip()
    return df


# ─── Section 1: Test MP ───────────────────────────────────────────────────────

def analyse_test_mp(df, writer=None):
    banner("SECTION 1 — Test MP (Customer Master)")
    n = len(df)
    print(f"  Total records : {n:,}")
    print(f"  Extracted at  : {df['_extracted_at'].max()}")

    # Rename ugly Celonis expression column
    cust_mat_desc_col = "#{o_custom_CustomerRoleMaterial.CustomerMaterialDescription}"
    df = df.rename(columns={cust_mat_desc_col: "customer_material_description"})

    # ── Field-level completeness ──────────────────────────────────────────────
    sub("Field Completeness (all records)")
    checks = [
        ("customer_material_number",      "Customer Material Number"),
        ("customer_material_description", "Customer Material Description"),
        ("material_description",          "Internal Material Description"),
        ("sold_to_id",                    "Sold-To ID"),
        ("sold_to_name_full",             "Sold-To Name"),
        ("sold_to_postcode",              "Sold-To Postal Code"),
        ("ship_to_id",                    "Ship-To ID"),
        ("ship_to_name_full",             "Ship-To Name"),
        ("ship_to_postcode",              "Ship-To Postal Code"),
        ("salesperson_name",              "CSR / Salesperson Name"),
        ("salesperson_email",             "CSR / Salesperson Email"),
        ("sales_organization",            "Sales Organization"),
        ("customer_group2",               "Customer Group 2"),
    ]

    summary_rows = []
    for col, label in checks:
        if col not in df.columns:
            print(f"    [SKIP] Column '{col}' not in dataset")
            continue
        missing = is_empty(df[col]).sum()
        present = n - missing
        row_stat(label, missing, n)
        summary_rows.append({
            "Field": label, "Column": col,
            "Total": n, "Missing": int(missing), "Present": int(present),
            "Missing_Pct": round(missing / n * 100, 2) if n else 0,
        })

    # ── By Sales Organization ─────────────────────────────────────────────────
    sub("Missing Fields — by Sales Organization")
    grp = df.groupby("sales_organization").agg(
        total=("sold_to_id", "count"),
        missing_cust_mat_no=("customer_material_number",
                             lambda s: int(is_empty(s).sum())),
        missing_cust_mat_desc=("customer_material_description",
                               lambda s: int(is_empty(s).sum())),
        missing_sold_to_postcode=("sold_to_postcode",
                                  lambda s: int(is_empty(s).sum())),
        missing_ship_to_postcode=("ship_to_postcode",
                                  lambda s: int(is_empty(s).sum())),
        missing_csr_name=("salesperson_name",
                          lambda s: int(is_empty(s).sum())),
        missing_csr_email=("salesperson_email",
                           lambda s: int(is_empty(s).sum())),
    ).reset_index().sort_values("total", ascending=False)

    grp["pct_no_cust_mat_no"]  = (grp["missing_cust_mat_no"]     / grp["total"] * 100).round(1)
    grp["pct_no_sold_postcode"]= (grp["missing_sold_to_postcode"] / grp["total"] * 100).round(1)
    grp["pct_no_csr_email"]    = (grp["missing_csr_email"]        / grp["total"] * 100).round(1)

    print(f"  {'SalesOrg':<10} {'Total':>9} {'NoCustMatNo':>12} {'%':>6}"
          f" {'NoSoldPostcode':>15} {'%':>6} {'NoCsrEmail':>11} {'%':>6}")
    print("  " + "-" * 80)
    for _, r in grp.head(25).iterrows():
        print(f"  {r['sales_organization']:<10} {r['total']:>9,}"
              f" {r['missing_cust_mat_no']:>12,} {r['pct_no_cust_mat_no']:>6.1f}%"
              f" {r['missing_sold_to_postcode']:>15,} {r['pct_no_sold_postcode']:>6.1f}%"
              f" {r['missing_csr_email']:>11,} {r['pct_no_csr_email']:>6.1f}%")

    # ── Unique counts ─────────────────────────────────────────────────────────
    sub("Unique Entity Counts")
    print(f"    Unique Sold-To IDs       : {df['sold_to_id'].nunique():,}")
    print(f"    Unique Ship-To IDs       : {df['ship_to_id'].nunique():,}")
    print(f"    Unique Materials (int.)  : {df['material_internal'].nunique():,}")
    print(f"    Unique Sales Orgs        : {df['sales_organization'].nunique():,}")
    print(f"    Unique CSR Emails        : {df['salesperson_email'].nunique():,}")
    print(f"    Unique Cust. Mat. Nos.   : {df['customer_material_number'].nunique():,}")

    # ── Customers with zero CSR email ─────────────────────────────────────────
    sub("Sold-To accounts with NO CSR email at all")
    sold_to_csr = (df.groupby("sold_to_id")
                   .agg(rows=("material_internal", "count"),
                        has_email=("salesperson_email",
                                   lambda s: int((~is_empty(s)).any())))
                   .reset_index())
    no_email = sold_to_csr[sold_to_csr["has_email"] == 0]
    print(f"    Sold-To accounts with zero CSR email: {len(no_email):,}"
          f"  (of {len(sold_to_csr):,} unique sold-to accounts)")
    if len(no_email) <= 30:
        print(f"    IDs: {list(no_email['sold_to_id'])}")

    # ── Duplicate customer material numbers ───────────────────────────────────
    sub("Ambiguous Mappings: 1 Customer Mat# -> >1 Internal Material")
    dups = (df[~is_empty(df["customer_material_number"])]
            .groupby(["sold_to_id", "customer_material_number"])["material_internal"]
            .nunique()
            .reset_index()
            .rename(columns={"material_internal": "unique_materials"}))
    multi = dups[dups["unique_materials"] > 1]
    print(f"    {len(multi):,} (sold_to + CustMatNo) combos map to >1 internal material")
    if len(multi) > 0:
        for _, r in multi.sort_values("unique_materials", ascending=False).head(10).iterrows():
            print(f"      Sold-To: {r['sold_to_id']}  "
                  f"CustMatNo: {r['customer_material_number']:<30} "
                  f"-> {r['unique_materials']} materials")

    # ── Excel output ──────────────────────────────────────────────────────────
    if writer:
        pd.DataFrame(summary_rows).to_excel(
            writer, sheet_name="TestMP_Completeness", index=False)
        grp.to_excel(writer, sheet_name="TestMP_BySalesOrg", index=False)
        no_email.to_excel(writer, sheet_name="TestMP_NoCSREmail", index=False)
        multi.sort_values("unique_materials", ascending=False).head(500).to_excel(
            writer, sheet_name="TestMP_AmbiguousMappings", index=False)

    return df  # return with renamed column


# ─── Section 2: Sheet 2 ───────────────────────────────────────────────────────

def analyse_sheet2(df, writer=None):
    banner("SECTION 2 — Sheet 2 (Customer Master — New Sheet 2)")
    n = len(df)
    print(f"  Total records : {n:,}")
    print(f"  Extracted at  : {df['_extracted_at'].max()}")

    sub("Field Completeness")
    checks = [
        ("customer_material_number",      "Customer Material Number"),
        ("customer_material_description", "Customer Material Description"),
        ("material_description",          "Internal Material Description"),
        ("sold_to",                       "Sold-To"),
        ("ship_to",                       "Ship-To"),
        ("ship_to_name",                  "Ship-To Name"),
        ("salesperson_email",             "CSR / Salesperson Email"),
        ("customer_group2",               "Customer Group 2"),
        ("country",                       "Country"),
        ("city",                          "City"),
    ]

    summary_rows = []
    for col, label in checks:
        if col not in df.columns:
            continue
        missing = is_empty(df[col]).sum()
        present = n - missing
        row_stat(label, missing, n)
        summary_rows.append({
            "Field": label, "Column": col,
            "Total": n, "Missing": int(missing), "Present": int(present),
            "Missing_Pct": round(missing / n * 100, 2) if n else 0,
        })

    sub("Missing Fields — by Sales Organization")
    grp = df.groupby("sales_organization").agg(
        total=("customer_id", "count"),
        missing_cust_mat_no=("customer_material_number",
                             lambda s: int(is_empty(s).sum())),
        missing_csr_email=("salesperson_email",
                           lambda s: int(is_empty(s).sum())),
        missing_ship_to=("ship_to",
                         lambda s: int(is_empty(s).sum())),
    ).reset_index().sort_values("total", ascending=False)
    grp["pct_no_cust_mat_no"] = (grp["missing_cust_mat_no"] / grp["total"] * 100).round(1)
    grp["pct_no_csr_email"]   = (grp["missing_csr_email"]   / grp["total"] * 100).round(1)
    grp["pct_no_ship_to"]     = (grp["missing_ship_to"]     / grp["total"] * 100).round(1)

    print(f"  {'SalesOrg':<10} {'Total':>10} {'NoCustMatNo':>12} {'%':>6}"
          f" {'NoCsrEmail':>11} {'%':>6} {'NoShipTo':>9} {'%':>6}")
    print("  " + "-" * 75)
    for _, r in grp.head(25).iterrows():
        print(f"  {r['sales_organization']:<10} {r['total']:>10,}"
              f" {r['missing_cust_mat_no']:>12,} {r['pct_no_cust_mat_no']:>6.1f}%"
              f" {r['missing_csr_email']:>11,} {r['pct_no_csr_email']:>6.1f}%"
              f" {r['missing_ship_to']:>9,} {r['pct_no_ship_to']:>6.1f}%")

    if writer:
        pd.DataFrame(summary_rows).to_excel(
            writer, sheet_name="Sheet2_Completeness", index=False)
        grp.to_excel(writer, sheet_name="Sheet2_BySalesOrg", index=False)


# ─── Section 3: Sheet 3 ───────────────────────────────────────────────────────

def analyse_sheet3(df, writer=None):
    banner("SECTION 3 — Sheet 3 (Historical Order Mapping)")
    n = len(df)
    print(f"  Total records : {n:,}")
    print(f"  Extracted at  : {df['_extracted_at'].max()}")

    sub("Field Completeness")
    checks = [
        ("customer_id",        "Customer ID"),
        ("material_internal",  "Internal Material"),
        ("customer_po_number", "Customer PO Number"),
        ("sales_order",        "Sales Order Number"),
        ("creation_date",      "Creation Date"),
    ]
    summary_rows = []
    for col, label in checks:
        if col not in df.columns:
            continue
        missing = is_empty(df[col]).sum()
        present = n - missing
        row_stat(label, missing, n)
        summary_rows.append({
            "Field": label, "Column": col,
            "Total": n, "Missing": int(missing), "Present": int(present),
            "Missing_Pct": round(missing / n * 100, 2) if n else 0,
        })

    sub("Unique Entity Counts")
    print(f"    Unique Customers         : {df['customer_id'].nunique():,}")
    print(f"    Unique Materials         : {df['material_internal'].nunique():,}")
    print(f"    Unique Sales Orders      : {df['sales_order'].nunique():,}")
    print(f"    Unique Sales Orgs        : {df['sales_organization'].nunique():,}")

    sub("Volume by Sales Organization")
    grp = (df.groupby("sales_organization")
             .agg(order_count=("sales_order", "count"),
                  unique_customers=("customer_id", "nunique"),
                  unique_materials=("material_internal", "nunique"))
             .reset_index()
             .sort_values("order_count", ascending=False))
    print(f"  {'SalesOrg':<10} {'Orders':>9} {'UniqueCustomers':>16} {'UniqueMaterials':>16}")
    print("  " + "-" * 55)
    for _, r in grp.head(25).iterrows():
        print(f"  {r['sales_organization']:<10} {r['order_count']:>9,}"
              f" {r['unique_customers']:>16,} {r['unique_materials']:>16,}")

    if writer:
        pd.DataFrame(summary_rows).to_excel(
            writer, sheet_name="Sheet3_Completeness", index=False)
        grp.to_excel(writer, sheet_name="Sheet3_BySalesOrg", index=False)


# ─── Section 4: SO Creation Results ─────────────────────────────────────────

def analyse_so_results(df, writer=None):
    banner("SECTION 4 — SO Creation Results (Celonis Action Flow Output)")
    n = len(df)
    print(f"  Total records : {n:,}")
    if n == 0:
        print("  [INFO] No SO results yet.")
        return

    sub("Status Breakdown")
    status_counts = df["STATUS"].value_counts()
    for status, count in status_counts.items():
        row_stat(status, int(count), n)

    sub("By Sales Organization")
    grp = df.groupby(["SALES_ORG", "STATUS"]).size().reset_index(name="count")
    pivot = grp.pivot_table(index="SALES_ORG", columns="STATUS",
                            values="count", fill_value=0).reset_index()
    print(pivot.to_string(index=False))

    sub("Failed / Blocked Details")
    failed = df[df["STATUS"].isin(["FAILED", "BLOCKED"])]
    if failed.empty:
        print("    No FAILED or BLOCKED records.")
    else:
        for _, r in failed.iterrows():
            reason = str(r.get("FAILURE_REASON") or r.get("BLOCK_REASON", ""))
            print(f"    PO: {r['PO_NUMBER']:<20}  Status: {r['STATUS']:<10}"
                  f"  Reason: {reason[:70]}")

    sub("Recent 10 SO Results")
    recent = df.sort_values("RUN_TIMESTAMP", ascending=False).head(10)
    for _, r in recent.iterrows():
        print(f"    {str(r['RUN_TIMESTAMP'])[:19]}  SO: {r['SO_NUMBER']:<14}"
              f"  PO: {r['PO_NUMBER']:<20}  Status: {r['STATUS']}")

    if writer:
        df.to_excel(writer, sheet_name="SO_Results", index=False)
        pivot.to_excel(writer, sheet_name="SO_Results_ByOrg", index=False)


# ─── Section 5: CSR Coverage cross-dataset ───────────────────────────────────

def analyse_csr_coverage(df_testmp, df_sheet2, writer=None):
    banner("SECTION 5 — CSR / Salesperson Email Coverage (Cross-Dataset)")

    if df_testmp is not None and "salesperson_email" in df_testmp.columns:
        sub("Test MP — CSR Email Coverage at Sold-To Level")
        sold_to_level = df_testmp.drop_duplicates(subset=["sold_to_id"]).copy()
        n = len(sold_to_level)
        missing = is_empty(sold_to_level["salesperson_email"]).sum()
        print(f"    Unique Sold-To records    : {n:,}")
        print(f"    WITH CSR email            : {n - missing:,}  ({pct(n - missing, n)})")
        print(f"    WITHOUT CSR email         : {missing:,}  ({pct(missing, n)})")

        # Tag missing email flag BEFORE groupby (avoids lambda returning scalar)
        sold_to_level["_no_email"] = is_empty(sold_to_level["salesperson_email"]).astype(int)
        grp = (sold_to_level
               .groupby("sales_organization")
               .agg(sold_to_count=("sold_to_id", "count"),
                    missing_email=("_no_email", "sum"))
               .reset_index())
        grp["pct_missing"] = (grp["missing_email"] / grp["sold_to_count"] * 100).round(1)
        grp = grp.sort_values("pct_missing", ascending=False)

        print(f"\n    {'SalesOrg':<12} {'SoldToCount':>12} {'NoEmail':>9} {'Missing%':>10}")
        print("    " + "-" * 50)
        for _, r in grp.iterrows():
            print(f"    {r['sales_organization']:<12} {r['sold_to_count']:>12,}"
                  f" {r['missing_email']:>9,} {r['pct_missing']:>9.1f}%")

        # Sold-to with NO email at all (across all materials)
        sub("Sold-To accounts with zero CSR email across all materials")
        mp_full = df_testmp.copy()
        mp_full["_no_email"] = is_empty(mp_full["salesperson_email"]).astype(int)
        mp_full["_has_email"] = (~is_empty(mp_full["salesperson_email"])).astype(int)
        csr_grp = (mp_full.groupby("sold_to_id")
                   .agg(rows=("material_internal", "count"),
                        has_email=("_has_email", "sum"))
                   .reset_index())
        no_email_ids = csr_grp[csr_grp["has_email"] == 0]
        print(f"    Sold-To with ZERO CSR email: {len(no_email_ids):,}"
              f"  (of {len(csr_grp):,} unique sold-to accounts)")

        if writer:
            grp.to_excel(writer, sheet_name="CSR_Coverage_BySalesOrg", index=False)
            no_email_ids.to_excel(writer, sheet_name="TestMP_NoCSREmail_SoldTo", index=False)

    if df_testmp is not None and df_sheet2 is not None:
        sub("Sold-To IDs in Sheet 2 NOT found in Test MP")
        s2_ids   = set(df_sheet2["sold_to"].dropna().astype(str).str.strip().unique())
        mp_ids   = set(df_testmp["sold_to_id"].dropna().astype(str).str.strip().unique())
        unmapped = s2_ids - mp_ids
        print(f"    Sheet 2 unique sold-to IDs  : {len(s2_ids):,}")
        print(f"    Test MP unique sold-to IDs  : {len(mp_ids):,}")
        print(f"    In Sheet 2 but NOT in Test MP: {len(unmapped):,}")
        if 0 < len(unmapped) <= 50:
            print(f"    IDs: {sorted(unmapped)}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Celonis EDA — Data Quality Report for Client Presentation")
    parser.add_argument("--no-excel", action="store_true",
                        help="Skip Excel output, print to console only")
    args = parser.parse_args()

    print("\n" + "=" * 72)
    print("  CELONIS DATA QUALITY — EXPLORATORY DATA ANALYSIS")
    print(f"  Generated : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Cache dir : {CACHE_DIR.resolve()}")
    print("=" * 72)

    print("\n  Loading datasets...")
    df_testmp = load("test_mp_customer_master.parquet")
    df_sheet2 = load("sheet2_customer_master.parquet")
    df_sheet3 = load("sheet3_order_mapping.parquet")
    df_so     = load("so_creation_results.parquet")

    for name, df in [("Test MP", df_testmp), ("Sheet 2", df_sheet2),
                     ("Sheet 3", df_sheet3), ("SO Results", df_so)]:
        status = f"{len(df):,} rows" if df is not None else "NOT FOUND"
        print(f"    {name:<20}: {status}")

    writer = None
    if not args.no_excel:
        try:
            writer = pd.ExcelWriter(str(OUTPUT_XLS), engine="xlsxwriter")
            print(f"\n  Excel output: {OUTPUT_XLS.name}")
        except Exception as e:
            print(f"  [WARN] Excel writer failed ({e}) — run: pip install xlsxwriter")

    # ── Run analyses ──────────────────────────────────────────────────────────
    df_testmp_renamed = None
    if df_testmp is not None:
        df_testmp_renamed = analyse_test_mp(df_testmp, writer)

    if df_sheet2 is not None:
        analyse_sheet2(df_sheet2, writer)

    if df_sheet3 is not None:
        analyse_sheet3(df_sheet3, writer)

    if df_so is not None:
        analyse_so_results(df_so, writer)

    analyse_csr_coverage(df_testmp_renamed, df_sheet2, writer)

    if writer:
        try:
            writer.close()
            print(f"\n  [OK] Excel report saved -> {OUTPUT_XLS.resolve()}")
        except Exception as e:
            print(f"  [WARN] Could not save Excel: {e}")

    print("\n" + "=" * 72)
    print("  EDA COMPLETE")
    print("=" * 72 + "\n")


if __name__ == "__main__":
    main()
