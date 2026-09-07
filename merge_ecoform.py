import json

# Load main merged dataset
main_records = []
with open('results_merged.jsonl', encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if line:
            try:
                main_records.append(json.loads(line))
            except:
                pass

# Load the new Ecoform extraction
ecoform_records = []
with open('ecoform_reextract.jsonl', encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if line:
            try:
                ecoform_records.append(json.loads(line))
            except:
                pass

print(f"Main records: {len(main_records)}")
print(f"Ecoform new records: {len(ecoform_records)}")

if ecoform_records:
    new_src = ecoform_records[0].get('source_file', '')
    print(f"New Ecoform source_file: {new_src}")

# Replace old Ecoform record with re-extracted one
updated = 0
filtered = []
for r in main_records:
    src = r.get('source_file', '')
    h = r.get('header_fields') or {}
    po = str(h.get('po_number', ''))
    if '4674421' in po or ('ecoform' in src.lower() and not r.get('raw_text')):
        print(f"  -> Replacing old record: {src}")
        updated += 1
    else:
        filtered.append(r)

# Add new Ecoform records
final = filtered + ecoform_records

print(f"Replaced {updated} old records. Total now: {len(final)}")

# Write updated merged file
with open('results_merged.jsonl', 'w', encoding='utf-8') as f:
    for r in final:
        f.write(json.dumps(r, ensure_ascii=False) + '\n')

print("Updated results_merged.jsonl saved.")
