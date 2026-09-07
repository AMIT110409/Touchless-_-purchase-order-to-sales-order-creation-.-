
import json

def check_raw():
    try:
        data = []
        with open('results_paddle_v9.jsonl', 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    data.append(json.loads(line))
        
        stebro_records = [r for r in data if 'STEBRO' in str(r).upper()]
        
        print(f"Stebro Raw Records: {len(stebro_records)}")
        
        for r in stebro_records:
            print(f"File: {r.get('source_file')}")
            for i in r.get('line_items', []):
                qty = i.get('quantity')
                print(f"  Raw Qty: {repr(qty)}")

    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    check_raw()
