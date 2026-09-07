
import json
import sys

def check():
    try:
        data = json.load(open('final_output_v11.json', encoding='utf-8'))
        fox_records = [r for r in data if 'FOXCONN' in str(r).upper()]
        
        print(f"Foxconn Records: {len(fox_records)}")
        
        for r in fox_records:
            print(f"File: {r.get('source_file')}")
            for i in r.get('line_items', []):
                mat = i.get('material_code')
                internal = i.get('internal_material')
                so = r['header_fields'].get('sales_organization')
                
                status = "OK" if internal else "MISSING"
                
                print(f"  SalesOrg: {so} | Mat: '{mat}' | Internal: '{internal}' | Status: {status}")
                if status == "MISSING" and mat:
                    # Print checks
                    print(f"    -> Hex: {mat.encode('utf-8').hex()}")
                    print(f"    -> Repr: {repr(mat)}")

    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    check()
