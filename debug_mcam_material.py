"""
Debug internal material mapping for MCAM PO 849474.
Checks what internal_material_number gets mapped for customer 4020042625 + material 06030000976.
"""
import sys, io, json, os, glob
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# ── Step 1: Show what the CURRENT enriched JSONL has ────────────────────────
print("=" * 60)
print("STEP 1: Current enriched JSONL sales_orders for PO 849474")
print("=" * 60)
for jf in sorted(glob.glob('results_*_enriched.jsonl'), key=os.path.getmtime, reverse=True):
    with open(jf, 'r', encoding='utf-8') as fh:
        for line in fh:
            try:
                r = json.loads(line)
                h = r.get('header_fields', {})
                if '849474' in str(h.get('po_number', '')):
                    print(f"File: {jf}")
                    print(f"  customer_number  : {h.get('customer_number')}")
                    print(f"  sold_to_id       : {h.get('sold_to_id')}")
                    sos = r.get('sales_orders', [])
                    for i, so in enumerate(sos):
                        print(f"  Sales Order [{i+1}]:")
                        items = so.get('items', [so]) if 'items' in so else [so]
                        # SO may be flat or have items
                        internal_mat = so.get('internal_material_number', '(not in so root)')
                        ext_mat = so.get('extracted_material_number', '')
                        cust_mat = so.get('customer_material_number', '')
                        print(f"    internal_material_number : {internal_mat}")
                        print(f"    extracted_material_number: {ext_mat}")
                        print(f"    customer_material_number : {cust_mat}")
                    # Also show line_items
                    print("  line_items:")
                    for li in r.get('line_items', []):
                        mc = li.get('material_code', '')
                        md = li.get('material_description', '')
                        print(f"    material_code={mc!r}  desc={md!r}")
            except Exception as e:
                pass

# ── Step 2: Check Test MP for what internal material maps to ────────────────
print()
print("=" * 60)
print("STEP 2: Test MP lookup for customer 4020042625 + material 06030000976")
print("=" * 60)
from sales_order_mapper import SalesOrderMapper
mapper = SalesOrderMapper.from_azure(force_refresh=False)

CORRECT_CUSTOMER = "4020042625"
WRONG_CUSTOMER   = "4020000669"  # what old enrichment used

for cust in [CORRECT_CUSTOMER, WRONG_CUSTOMER]:
    result = mapper.lookup(cust, "06030000976", "")
    print(f"\nlookup(customer={cust}, material='06030000976'):")
    if result:
        print(f"  internal_material_number : {result.get('internal_material_number')}")
        print(f"  customer_material_number : {result.get('customer_material_number')}")
        print(f"  sales_organization       : {result.get('sales_organization')}")
        print(f"  order_type               : {result.get('order_type')}")
        print(f"  is_global_fallback       : {result.get('is_global_fallback')}")
    else:
        print(f"  => NO MATCH (returns None)")

# ── Step 3: Show all materials for customer 4020042625 in Test MP ────────────
print()
print("=" * 60)
print("STEP 3: All materials in Test MP for customer 4020042625")
print("=" * 60)
import pandas as pd
df = pd.read_parquet(os.path.join('.celonis_cache', 'test_mp_customer_master.parquet'))
cust_rows = df[df['sold_to_id'].astype(str).str.strip() == CORRECT_CUSTOMER]
mat_cols = ['customer_material_number', 'material_internal', 'sales_organization']
avail = [c for c in mat_cols if c in cust_rows.columns]
unique_mats = cust_rows[avail].drop_duplicates()
print(unique_mats.to_string(index=False))
