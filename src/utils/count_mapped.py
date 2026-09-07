import json, sys
sys.stdout.reconfigure(encoding='utf-8')

with open('final_output_v8.json', encoding='utf-8') as f:
    data = json.load(f)

mapped   = 0
not_found_no_excel  = 0
not_found_mismatch  = 0
no_mat_code = 0

import pandas as pd
df = pd.read_excel('EXPORT_20260216_100124.XLSX', dtype=str, engine='openpyxl')
excel_ids = set()
for _, row in df.iterrows():
    cid = str(row.get('Customer','')).strip()
    if cid.endswith('.0'): cid = cid[:-2]
    if cid and cid.lower() != 'nan': excel_ids.add(cid)

rows_missing = []
for r in data:
    if 'error' in r: continue
    h = r.get('header_fields', {})
    sold_to = h.get('sold_to_id','') or ''
    cname   = h.get('customer_id_or_name','?')
    for item in r.get('line_items',[]):
        mc  = item.get('material_code')
        im  = item.get('internal_material')
        cmn = item.get('customer_material_number')
        if im:
            mapped += 1
        elif not mc:
            no_mat_code += 1
        elif sold_to not in excel_ids:
            not_found_no_excel += 1
            rows_missing.append(f'  [NOT-IN-EXCEL] {cname:<38} Sold-To={sold_to}  ExtractedCode={mc}')
        else:
            not_found_mismatch += 1
            rows_missing.append(f'  [CODE-MISMATCH] {cname:<37} Sold-To={sold_to}  ExtractedCode={mc}')

total = mapped + not_found_no_excel + not_found_mismatch + no_mat_code
print('='*70)
print(f'TOTAL LINE ITEMS : {total}')
print(f'MAPPED (internal OK) : {mapped}  ({100*mapped//total}%)')
print(f'NO MAT CODE in PDF   : {no_mat_code}')
print(f'CUSTOMER NOT IN EXCEL: {not_found_no_excel}')
print(f'CODE MISMATCH        : {not_found_mismatch}')
print('='*70)
print()
for line in rows_missing:
    print(line)
