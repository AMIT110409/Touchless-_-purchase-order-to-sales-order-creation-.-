import json
import csv
import argparse

def export_to_csv(input_file="results_po_full_enriched.jsonl", output_file="extracted_pos_full.csv"):
    # Read output
    with open(input_file, "r", encoding="utf-8") as f:
        data = [json.loads(line) for line in f if line.strip()]
    
    tracker = None
    try:
        from azure_email_tracker import AzureEmailTracker
        tracker = AzureEmailTracker()
    except Exception:
        pass

    with open(output_file, "w", encoding="utf-8-sig", newline="") as out_f:
        writer = csv.writer(out_f)
        writer.writerow([
            "PO Number", "Order Date", "Requested Delivery Date", "Customer Name", "Vendor Name",
            "Sold To ID", "Sales Org", "Order Type", "SO Rule", "Ship To ID", "Ship To Name",
            "Customer Number", "Material Description", "Quantity", "Unit", "Delivery Date",
            "Internal Material Code", "Customer Material Code", "Extracted Material Code",
            "Customer Group 2", "SO Grouping Rule", "Employee Responsible", "Email Responsible", "Sender Email", "Source File"
        ])
        
        seen_row_keys = set()
        for file_res in data:
            source_file = file_res.get("source_file", "")
            if "error" in file_res and "header_fields" not in file_res:
                # Still output an error row so the user knows this file was processed but failed OCR
                writer.writerow([
                    "ERROR", "", "", "", "", "", "", "", "", "", "", "", 
                    file_res.get("error", "Failed extraction"), "", "", "", "", "", "",
                    "", "", "", "", "", source_file
                ])
                continue
            
            sales_orders = file_res.get("sales_orders", [])
            header = file_res.get("header_fields", {})
            vendor_for_display = header.get("vendor_name", "Envalior B.V.")
            hdr_sender_email = header.get("sender_email", "") or header.get("email_sender", "") or ""
            
            for so in sales_orders:
                po = so.get("po_number", "")
                od = so.get("order_date", "")
                rdd = so.get("requested_delivery_date", "")
                cn_name = so.get("customer_name", "")
                slr = so.get("sales_organization", "")
                ot = so.get("order_type", "")
                sold_to = so.get("sold_to_id", "")
                ship_to = so.get("ship_to_id", "")
                ship_name = so.get("ship_to_name", "")
                cust_num = so.get("customer_number", "")
                so_rule   = so.get("so_split_rule", "X")
                cg2       = so.get("customer_group2", "")
                so_grouping = so.get("so_grouping_label", "")
                emp_resp  = so.get("employee_responsible", "")
                email_resp = so.get("email_responsible", "")
                snd_email  = so.get("sender_email", "") or hdr_sender_email
                if not snd_email and source_file and tracker:
                    try:
                        _ent = tracker.get_email_by_filename(source_file)
                        if _ent and _ent.get("sender_email"):
                            snd_email = _ent["sender_email"]
                    except Exception:
                        pass

                # Fallback to CSR Email Responsible if sender_email is not available
                if not snd_email:
                    snd_email = email_resp



                
                for item in so.get("items", []):
                    mat_desc = item.get("material_description", "")
                    qty = item.get("quantity", "")
                    unit = item.get("unit", "")
                    if unit:
                        try:
                            from sales_order_mapper import _normalise_unit
                            unit = _normalise_unit(unit)
                        except Exception:
                            pass
                    dd = item.get("delivery_date", "")
                    mat_internal = item.get("internal_material_number", "")
                    mat_cust = item.get("customer_material_number", "")
                    mat_extracted = item.get("extracted_material_number", "")
                    
                    # Key for row deduplication: (PO Number, Sold To, Ship To, Material Code/Desc, Quantity, Delivery Date)
                    mat_key = str(mat_internal or mat_extracted or mat_desc).strip().lower()
                    row_key = (
                        str(po).strip().lower(),
                        str(sold_to).strip(),
                        str(ship_to).strip(),
                        mat_key,
                        str(qty).strip(),
                        str(dd).strip()
                    )
                    if row_key in seen_row_keys and po:
                        continue
                    seen_row_keys.add(row_key)


                    writer.writerow([
                        po, od, rdd, cn_name, vendor_for_display, sold_to, slr, ot, so_rule,
                        ship_to, ship_name, cust_num, mat_desc, qty, unit, dd, 
                        mat_internal, mat_cust, mat_extracted, cg2, so_grouping,
                        emp_resp, email_resp, snd_email, source_file
                    ])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="results_po_full_enriched.jsonl")
    parser.add_argument("--output", default="extracted_pos_full.csv")
    args = parser.parse_args()

    import sys
    import os

    # -- Guard: input file must exist and be non-empty --
    if not os.path.exists(args.input):
        print(f"  [CSV Export] Input file not found: {args.input}")
        print(f"  [CSV Export] Skipping CSV generation — no enriched results yet.")
        sys.exit(0)   # exit 0 (not a hard error) so pipeline continues

    with open(args.input, "r", encoding="utf-8") as _f:
        _lines = [l for l in _f if l.strip()]
    if not _lines:
        print(f"  [CSV Export] Input file is empty: {args.input}")
        print(f"  [CSV Export] Skipping CSV generation — no data to export.")
        sys.exit(0)   # exit 0 so pipeline continues

    export_to_csv(args.input, args.output)
    print(f"  [CSV Export] Generated: {args.output}  ({len(_lines)} row(s) processed)")

    try:
        import pandas as pd
        excel_file = args.output.rsplit(".", 1)[0] + ".xlsx"
        df = pd.read_csv(args.output, encoding="utf-8-sig")
        df.to_excel(excel_file, index=False)
        print(f"  [CSV Export] Excel generated: {excel_file}")
    except ImportError:
        print("  [CSV Export] Install pandas and openpyxl to also generate an Excel file.")
    except Exception as e:
        print(f"  [CSV Export] Excel generation skipped: {e}")
