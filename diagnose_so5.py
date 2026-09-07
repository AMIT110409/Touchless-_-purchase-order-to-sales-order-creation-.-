import json

# Check all records - their source file and so_rule
data = json.load(open('final_output_fixed.json', encoding='utf-8'))
so_groups = json.load(open('final_output_fixed_so_groups.json', encoding='utf-8'))

# Files in SO groups
so_files = set(s['source_file'] for s in so_groups)

print("=== Files WITH SO groups ===")
for f in sorted(so_files):
    print(f"  {f}")

print()
print("=== Files WITHOUT SO groups (missing/skipped) ===")
for r in data:
    src = r.get('source_file', '')
    h = r.get('header_fields') or {}
    rule = h.get('so_grouping_rule', '')
    if src not in so_files:
        items = r.get('line_items', [])
        print(f"  {src}")
        print(f"    so_rule={repr(rule)} | sold_to={repr(h.get('sold_to_id'))} | cust_num={repr(h.get('customer_number'))}")
        for item in items:
            print(f"    Item: mat={repr(item.get('material_code'))} qty={item.get('quantity')} unit={repr(item.get('unit'))}")
