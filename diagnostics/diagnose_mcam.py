"""Diagnose MCAM (Mitsubishi Chemical Advanced Materials B.V.) PO mapping issue."""
import sys, io, json, os, glob

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# Check the most recent results JSONL files
jsonl_files = glob.glob('results_*.jsonl')
jsonl_files.sort(key=os.path.getmtime, reverse=True)
print('Most recent JSONL files:')
for f in jsonl_files[:5]:
    print(f'  {f} (modified: {os.path.getmtime(f):.0f})')

found = False
for jf in jsonl_files:
    with open(jf, 'r', encoding='utf-8') as fh:
        for line in fh:
            try:
                r = json.loads(line)
                h = r.get('header_fields', {})
                v_name = h.get('vendor_name', '') or h.get('supplier_name', '') or ''
                po = h.get('po_number', '')
                if 'mitsubishi' in v_name.lower() or 'mcam' in v_name.lower() or '849474' in str(po):
                    found = True
                    print(f'\n=== FOUND in {jf} ===')
                    print(f'  PO number       : {po}')
                    print(f'  vendor_name     : {v_name}')
                    customer_number = h.get('customer_number', '')
                    vendor_id = h.get('vendor_id', '')
                    ship_to_id = h.get('ship_to_id', '')
                    ship_to_name = h.get('ship_to_name', '')
                    ship_to_postcode = h.get('ship_to_postcode', '')
                    ship_to_address = h.get('ship_to_address', '')
                    src = r.get('source_file', '')
                    print(f'  customer_number : {customer_number}')
                    print(f'  vendor_id       : {vendor_id}')
                    print(f'  ship_to_id      : {ship_to_id}')
                    print(f'  ship_to_name    : {ship_to_name}')
                    print(f'  ship_to_postcode: {ship_to_postcode}')
                    print(f'  ship_to_address : {ship_to_address}')
                    print(f'  source_file     : {src}')
                    # Print all header fields for full debug
                    print(f'\n  Full header_fields:')
                    for k, v in h.items():
                        print(f'    {k}: {repr(v)}')
                    # Print sales orders
                    sos = r.get('sales_orders', [])
                    print(f'\n  Sales orders ({len(sos)}):')
                    for so in sos:
                        sold_to = so.get('sold_to_id', '')
                        ship_to = so.get('ship_to_id', '')
                        ship_nm = so.get('ship_to_name', '')
                        print(f'    sold_to_id={sold_to}  ship_to_id={ship_to}  ship_to_name={ship_nm}')
            except Exception as e:
                pass

if not found:
    print('\nNo MCAM/Mitsubishi POs found in JSONL files.')
    print('Checking line items for material 06030000976:')
    for jf in jsonl_files:
        with open(jf, 'r', encoding='utf-8') as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                    items = r.get('line_items', [])
                    for item in items:
                        mcode = item.get('material_code', '') or item.get('customer_material_number', '')
                        if '06030000976' in str(mcode) or '53398' in str(mcode):
                            h = r.get('header_fields', {})
                            po = h.get('po_number', '')
                            v_name = h.get('vendor_name', '')
                            print(f'  Found material in {jf}: PO={po}, vendor={v_name}, mat={mcode}')
                except:
                    pass
