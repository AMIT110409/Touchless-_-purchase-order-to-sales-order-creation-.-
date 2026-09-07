import json

# Check 1: Stebro in SO groups
so_groups = json.load(open('final_output_fixed_so_groups.json', encoding='utf-8'))
stebro_sos = [s for s in so_groups if 'stebro' in s['source_file'].lower() or '4020032023' in str(s['customer_id'])]
print(f"Stebro SOs in so_groups.json: {len(stebro_sos)}")

# Check 2: Stebro in enriched output
enriched = json.load(open('final_output_fixed.json', encoding='utf-8'))
stebro_recs = [r for r in enriched if 'stebro' in r.get('source_file','').lower() or '4020032023' in str((r.get('header_fields') or {}).get('sold_to_id',''))]
print(f"\nStebro records in enriched output: {len(stebro_recs)}")
for r in stebro_recs:
    h = r.get('header_fields') or {}
    print(f"  File: {r['source_file']}")
    print(f"  so_grouping_rule: {h.get('so_grouping_rule')!r}")
    print(f"  sales_org: {h.get('sales_organization')!r}")

# Check 3: Ecoform PO 4674421
print("\n--- Ecoform PO 4674421 ---")
ecoform = [r for r in enriched if '4674421' in str((r.get('header_fields') or {}).get('po_number','')) or '4674421' in r.get('source_file','')]
for r in ecoform:
    h = r.get('header_fields') or {}
    print(f"  File: {r['source_file']}")
    print(f"  PO: {h.get('po_number')}")
    print(f"  so_grouping_rule: {h.get('so_grouping_rule')!r}")
    for item in r.get('line_items', []):
        print(f"  Item: mat={item.get('material_code')} qty={item.get('quantity')} unit={item.get('unit')} internal={item.get('internal_material')} cust_mat={item.get('customer_material_number')}")
