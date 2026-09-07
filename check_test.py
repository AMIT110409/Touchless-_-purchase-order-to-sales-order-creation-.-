import json

try:
    with open('test_unmapped_results.jsonl', 'r', encoding='utf-8') as f:
        data = [json.loads(line) for line in f if line.strip()]
        
    for d in data:
        if 'error' in d: continue
        print(f"\n--- {d.get('source_file')} ---")
        head = d.get('header_fields', {})
        print(f"Extracted customer text: '{head.get('customer_id_or_name')}'")
        print(f"Matched KB Name:       '{head.get('customer_name_matched', 'None')}'")
        print(f"Matched Customer ID:   '{head.get('customer_number', d.get('matched_customer_id', 'None'))}'")
except Exception as e:
    print(e)
