import json, sys
sys.stdout.reconfigure(encoding='utf-8')

with open('final_output_v8.json', encoding='utf-8') as f:
    data = json.load(f)

total = len(data)
has_sales_org  = sum(1 for r in data if r.get('header_fields', {}).get('sales_organization') and r['header_fields']['sales_organization'] not in ('', 'nan', 'None'))
has_order_type = sum(1 for r in data if r.get('header_fields', {}).get('order_type') and r['header_fields']['order_type'] not in ('', 'nan', 'None'))
has_so_rule    = sum(1 for r in data if r.get('header_fields', {}).get('so_grouping_rule') and r['header_fields']['so_grouping_rule'] not in ('', 'nan', 'None'))
has_sold_to    = sum(1 for r in data if r.get('header_fields', {}).get('sold_to_id') and r['header_fields']['sold_to_id'] not in ('', 'nan', 'None'))
has_cust_name  = sum(1 for r in data if r.get('header_fields', {}).get('customer_id_or_name'))
has_po_num     = sum(1 for r in data if r.get('header_fields', {}).get('po_number'))

print(f'Total records: {total}')
print()
print('=== EXTRACTION ===')
print(f'PO Number:          {has_po_num}/{total} ({100*has_po_num//total}%)')
print(f'Customer Name:      {has_cust_name}/{total} ({100*has_cust_name//total}%)')
print(f'Sold-To ID:         {has_sold_to}/{total} ({100*has_sold_to//total}%)')
print()
print('=== EXCEL MAPPING (Updated EXPORT_20260216) ===')
print(f'Sales Organization: {has_sales_org}/{total} ({100*has_sales_org//total}%)')
print(f'Order Type:         {has_order_type}/{total} ({100*has_order_type//total}%)')
print(f'SO Grouping Rule:   {has_so_rule}/{total} ({100*has_so_rule//total}%)')
print()

# Per-customer summary
print('=== EXCEL MAPPING BY CUSTOMER ===')
print(f'{"Status":<8} {"Customer":<38} {"Sold-To":<14} {"SalesOrg":<10} {"OrderType":<11} {"SORule"}')
print('-'*95)

customer_map = {}
for r in data:
    h = r.get('header_fields', {})
    cname = h.get('customer_id_or_name', 'Unknown')
    sold  = h.get('sold_to_id', '')
    sorg  = h.get('sales_organization', '')
    otype = h.get('order_type', '')
    sorule= h.get('so_grouping_rule', '')
    if cname not in customer_map:
        customer_map[cname] = {'sold_to': sold, 'sales_org': sorg, 'order_type': otype, 'so_rule': sorule, 'count': 0}
    customer_map[cname]['count'] += 1

for cname, info in sorted(customer_map.items()):
    status = '[OK]    ' if info['sales_org'] else '[MISSING]'
    print(f'{status:<8} {cname[:37]:<38} {info["sold_to"]:<14} {info["sales_org"]:<10} {info["order_type"]:<11} {info["so_rule"]}')

print()
print('=== SAMPLE - First 5 Records ===')
for r in data[:5]:
    h = r.get('header_fields', {})
    print(f"  File: {r.get('source_file','')[:50]}")
    print(f"    PO#: {h.get('po_number','')} | Customer: {h.get('customer_id_or_name','')} | Sold-To: {h.get('sold_to_id','')}")
    print(f"    SalesOrg: {h.get('sales_organization','')} | OrderType: {h.get('order_type','')} | SORule: {h.get('so_grouping_rule','')}")
    print()
