
import json
import pandas as pd
import os
os.environ['DISABLE_MODEL_SOURCE_CHECK'] = 'True'

import sys

OUTPUT_JSON = "final_output_v10.json"
MAPPING_XLSX = "EXPORT_20260216_100124.XLSX"

def main():
    if not os.path.exists(OUTPUT_JSON):
        print(f"Error: {OUTPUT_JSON} not found.")
        return

    print(f"Analyzing {OUTPUT_JSON}...")
    
    records = []
    with open(OUTPUT_JSON, 'r', encoding='utf-8') as f:
        content = f.read().strip()
        if content.startswith('['):
            try:
                records = json.loads(content)
            except Exception as e:
                print(f"Error loading JSON array: {e}")
        else:
            # Fallback to lines
            for line in content.split('\n'):
                if line.strip():
                    try:
                        records.append(json.loads(line))
                    except: pass
    
    print(f"Total Records: {len(records)}")
    
    # Load Mapping for cross-check
    mapping_df = None
    if os.path.exists(MAPPING_XLSX):
        print(f"Loading mapping from {MAPPING_XLSX}...")
        try:
            mapping_df = pd.read_excel(MAPPING_XLSX, dtype=str, engine='openpyxl')
            # Normalize column names
            mapping_df.columns = [c.strip() for c in mapping_df.columns]
        except Exception as e:
            print(f"Error loading mapping: {e}")

    missing_so_count = 0
    missing_mat_count = 0
    
    missing_so_details = []
    missing_mat_details = []

    for i, rec in enumerate(records):
        if not isinstance(rec, dict): continue
        header = rec.get("header_fields", {})
        if not isinstance(header, dict):
            # Try to fix if string?
            header = {}
            
        line_items = rec.get("line_items", [])
        if not isinstance(line_items, list):
            line_items = []
            
        source_file = rec.get("source_file", "unknown")
        
        # Check Sales Org
        so = header.get("sales_organization")
        cust_id = header.get("customer_number") or header.get("sold_to_id") or header.get("customer_id_or_name")
        
        if not so:
            missing_so_count += 1
            missing_so_details.append({
                "file": source_file,
                "cust_id": cust_id,
                "reason": "Sales Org Empty"
            })
            
        # Check Line Items
        for item in line_items:
            if not isinstance(item, dict): continue
            internal_mat = item.get("internal_material")
            cust_mat = item.get("material_code")
            desc = item.get("description")
            
            if not internal_mat:
                missing_mat_count += 1
                missing_mat_details.append({
                    "file": source_file,
                    "cust_id": cust_id,
                    "cust_mat_extracted": cust_mat,
                    "desc_extracted": desc
                })

    print("-" * 40)
    print(f"Records Missing Sales Org: {missing_so_count}")
    if missing_so_count > 0:
        print("Details (first 10):")
        for d in missing_so_details[:10]:
            print(f"  File: {d['file']}")
            print(f"  Cust ID extracted: {d['cust_id']}")
            # Check if Cust ID exists in Excel
            if mapping_df is not None and d['cust_id']:
                matches = mapping_df[mapping_df['Customer'] == str(d['cust_id'])]
                if not matches.empty:
                    print(f"  -> FOUND in Excel! (Sales Org: {matches.iloc[0].get('Sales Organization')}) why failed?")
                else:
                    print(f"  -> NOT FOUND in Excel.")
            print("")

    print("-" * 40)
    print(f"Line Items Missing Internal Material: {missing_mat_count}")
    if missing_mat_count > 0:
        print("Details (first 5 unique materials):")
        seen = set()
        printed = 0
        for d in missing_mat_details:
            key = (d['cust_id'], d['cust_mat_extracted'])
            if key in seen: continue
            seen.add(key)
            
            print(f"  File: {d['file']}")
            print(f"  Cust ID: {d['cust_id']}")
            print(f"  Extracted Material: '{d['cust_mat_extracted']}'")
            print(f"  Description: '{d['desc_extracted']}'")
            
            # Check if Material exists in Excel for this customer
            if mapping_df is not None and d['cust_id'] and d['cust_mat_extracted']:
                # Filter by customer
                cust_rows = mapping_df[mapping_df['Customer'] == str(d['cust_id'])]
                if not cust_rows.empty:
                    # Check material column 'Customer Material Number'
                    # Try exact match, verify case
                    mat_match = cust_rows[cust_rows['Customer Material Number'] == str(d['cust_mat_extracted'])]
                    if not mat_match.empty:
                        print(f"    -> FOUND exact match in Excel! (Internal: {mat_match.iloc[0].get('Material')}) why failed?")
                    else:
                        # Try fuzzy/partial matches to see what's close
                        print(f"    -> NOT FOUND for this customer. Total materials for cust: {len(cust_rows)}")
                        # Print overlapping?
                else:
                    print(f"    -> Customer NOT FOUND in Excel.")
            print("")
            printed += 1
            if printed >= 5: break

if __name__ == "__main__":
    main()
