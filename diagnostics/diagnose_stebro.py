
import json

def check():
    try:
        data = json.load(open('final_output_FINAL.json', encoding='utf-8'))
        stebro_records = [r for r in data if 'STEBRO' in str(r).upper()]
        
        print(f"Stebro Records: {len(stebro_records)}")
        
        for r in stebro_records:
            print(f"File: {r.get('source_file')}")
            for i in r.get('line_items', []):
                qty = i.get('quantity')
                mat = i.get('material_code')
                desc = i.get('description')
                print(f"  Qty: {qty} | Mat: '{mat}' | Desc: '{desc}'")

    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    check()
