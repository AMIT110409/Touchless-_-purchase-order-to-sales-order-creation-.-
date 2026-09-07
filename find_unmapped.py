import json
import csv

input_file = 'results_po_full_enriched.jsonl'
unmapped_files = []
error_files = []

try:
    with open(input_file, 'r', encoding='utf-8') as f:
        for line in f:
            if not line.strip(): continue
            try:
                data = json.loads(line)
                source = data.get('source_file', 'Unknown')
                
                # Check for errors
                if 'error' in data:
                    error_files.append((source, data.get('error')))
                    continue
                
                # Check for unmapped customer
                header = data.get('header_fields', {})
                if not data.get('matched_customer_id'):
                    unmapped_files.append((source, "No Customer Match", header.get('customer_id_or_name', '')))
                    continue
                    
                # Check for completely unmapped materials (all items)
                items = data.get('line_items', [])
                all_unmapped = True
                for item in items:
                    if item.get('internal_material_number'):
                        all_unmapped = False
                        break
                
                if items and all_unmapped:
                    unmapped_files.append((source, "No Material Matches", ""))

            except json.JSONDecodeError:
                pass

    print(f"--- FAILED / ERROR FILES ({len(error_files)}) ---")
    for f, err in error_files:
         print(f"[{f}]: {err}")
         
    print(f"\n--- UNMAPPED FILES ({len(unmapped_files)}) ---")
    for f, reason, extracted in unmapped_files:
         if extracted:
             print(f"[{f}]: {reason} (Extracted text: '{extracted}')")
         else:
             print(f"[{f}]: {reason}")

except FileNotFoundError:
    print(f"File {input_file} not found. Please ensure extraction has been run.")
