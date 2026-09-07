
import json
import pandas as pd
import sys

def diagnose():
    try:
        data = json.load(open('final_output_v10.json', encoding='utf-8'))
        fox_records = [r for r in data if 'FOXCONN' in str(r).upper()]
        
        print(f"Foxconn Records Found: {len(fox_records)}")
        
        extracted_materials = []
        for r in fox_records:
            print(f"File: {r.get('source_file')}")
            for i in r.get('line_items', []):
                mat = i.get('material_code')
                extracted_materials.append(mat)
                print(f"  Extracted Mat: '{mat}' | Internal Mat: '{i.get('internal_material')}'")
        
        print("-" * 40)
        print("Checking Excel Master Data for Customer 4020010339...")
        
        df = pd.read_excel('EXPORT_20260216_100124.XLSX', dtype=str, engine='openpyxl')
        cust_rows = df[df['Customer'] == '4020010339']
        
        print(f"Total Excel Rows for Customer: {len(cust_rows)}")
        
        excel_materials = cust_rows['Customer Material Number'].tolist()
        internal_materials = cust_rows['Material'].tolist()
        
        print("Excel Entries (First 20):")
        for cm, im in zip(excel_materials[:20], internal_materials[:20]):
            print(f"  Cust Mat: '{cm}' -> Internal: '{im}'")
            
        print("-" * 40)
        print("Analysis:")
        for mat in extracted_materials:
            if not mat: continue
            clean_mat = str(mat).strip()
            # Check exact match
            if clean_mat in excel_materials:
                print(f"  '{clean_mat}' -> EXACT MATCH found in Excel!")
            else:
                # Check partial?
                found = False
                for cm in excel_materials:
                    if str(cm) in clean_mat or clean_mat in str(cm):
                        print(f"  '{clean_mat}' -> PARTIAL match with '{cm}'")
                        found = True
                if not found:
                    print(f"  '{clean_mat}' -> NO MATCH found.")

    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    diagnose()
