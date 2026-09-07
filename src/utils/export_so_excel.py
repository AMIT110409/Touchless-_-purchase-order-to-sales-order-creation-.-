import json
import pandas as pd

# Load SO groups
data = json.load(open('final_output_fixed_so_groups.json', encoding='utf-8'))

rows = []
for so in data:
    items = so.get('line_items', [])
    if not items:
        rows.append({
            "SO Number": so['so_number'],
            "Grouping Rule": so['grouping_rule'],
            "PO Number": so['po_number'],
            "Customer ID": so['customer_id'],
            "Sales Organization": so['sales_organization'],
            "Order Type": so['order_type'],
            "Material Code": None,
            "Description": None,
            "Quantity": None,
            "Unit": None,
            "Internal Material": None,
            "Customer Material": None,
            "Source File": so['source_file'],
        })
    else:
        for item in items:
            rows.append({
                "SO Number": so['so_number'],
                "Grouping Rule": so['grouping_rule'],
                "PO Number": so['po_number'],
                "Customer ID": so['customer_id'],
                "Sales Organization": so['sales_organization'],
                "Order Type": so['order_type'],
                "Material Code": item.get('material_code'),
                "Description": item.get('description'),
                "Quantity": item.get('quantity'),
                "Unit": item.get('unit'),
                "Internal Material": item.get('internal_material'),
                "Customer Material": item.get('customer_material_number'),
                "Source File": so['source_file'],
            })

df = pd.DataFrame(rows)
df.to_excel('so_groups.xlsx', index=False, engine='openpyxl')
print(f"Saved {len(data)} SOs ({len(rows)} line rows) to so_groups.xlsx")
