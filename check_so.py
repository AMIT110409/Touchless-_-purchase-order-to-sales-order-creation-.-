import json
d = json.load(open('final_output_fixed_so_groups.json', encoding='utf-8'))
print(f"Total SOs: {len(d)}\n")
for s in d:
    print(f"{s['so_number']} | Rule: {s['grouping_rule'][:30]} | PO: {s['po_number']} | Items: {len(s['line_items'])}")
