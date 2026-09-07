"""
Test full re-enrichment for MCAM PO 849474 to verify fix.
Uses the raw JSONL record and runs through the full enrichment pipeline.
"""
import sys, io, json, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# Load the raw (non-enriched) result for MCAM PO 849474
import glob
jsonl_files = glob.glob('results_*.jsonl')
jsonl_files.sort(key=os.path.getmtime, reverse=True)

target_record = None
for jf in jsonl_files:
    # Try the non-enriched file first
    if '_enriched' not in jf:
        with open(jf, 'r', encoding='utf-8') as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                    h = r.get('header_fields', {})
                    po = h.get('po_number', '')
                    src = r.get('source_file', '')
                    if '849474' in str(po) and 'ENVALIOR DEUTSCHLAND' in src:
                        target_record = r
                        print(f'Using record from: {jf}')
                        print(f'  source_file: {src}')
                        break
                except:
                    pass
    if target_record:
        break

if not target_record:
    print('ERROR: Could not find raw record for PO 849474')
    sys.exit(1)

# Reset enrichment fields (simulate fresh run)
h = target_record.get('header_fields', {})
for key in ['customer_number', 'customer_name_matched', 'vendor_id', 'vendor_name_matched',
            'ship_to_id', 'ship_to_name', 'sold_to_id', 'validation', 'customer_confidence',
            'customer_confidence_explanation', 'customer_warnings']:
    h.pop(key, None)

print(f'\nPre-enrichment state:')
print(f'  customer_name: {h.get("customer_name")}')
print(f'  customer_id: {h.get("customer_id")}')
print(f'  ship_to_postcode: {h.get("ship_to_postcode")}')

# Run enrichment
from reenrich_results import fuzzy_match_vendor, cross_validate_customer_match, validate_ship_sold_to
from reenrich_results import _load_vendor_cache
import re

_load_vendor_cache()

v_name = h.get("vendor_name") or h.get("supplier_name")
c_name = h.get("customer_name") or h.get("customer_id_or_name")

print(f'\nEnriching with customer_name="{c_name}", vendor_name="{v_name}"')

ship_to_pc = str(h.get("ship_to_postcode", "") or "").strip()
bill_to_pc = str(h.get("bill_to_postcode", "") or "").strip()
addr_parts = [
    h.get("bill_to_address", ""),
    h.get("sold_to_address", ""),
    bill_to_pc,
    h.get("country", ""),
    h.get("city", ""),
    h.get("ship_to_address", ""),
]
address_hint = " ".join(p for p in addr_parts if p).strip()

# Fuzzy match customer
c_match = fuzzy_match_vendor(c_name, line_items=target_record.get("line_items", []), address_hint=address_hint)
print(f'\nCustomer match result: {c_match}')

if c_match:
    customer_id = c_match["id"]
    print(f'\n=== RESOLVED customer_id: {customer_id} ===')
    print(f'  Expected: 4020042625')
    print(f'  Status: {"OK - CORRECT!" if customer_id == "4020042625" else "WRONG!"}')
    
    # Now check ship-to resolution
    from sales_order_mapper import SalesOrderMapper
    mapper = SalesOrderMapper.from_azure(force_refresh=False)
    mp_ship = mapper.get_ship_to_info_from_test_mp(
        customer_id=customer_id,
        ship_to_id='',
        ship_to_address=h.get('ship_to_address', ''),
        ship_to_postcode=ship_to_pc,
    )
    print(f'\nShip-to resolution:')
    print(f'  ship_to_id  : {mp_ship.get("ship_to_id")} (expected: 4020042625)')
    print(f'  ship_to_name: {mp_ship.get("ship_to_name")}')
    status = "[OK]" if mp_ship.get("ship_to_id") == "4020042625" else "[WRONG]"
    print(f'  Status: {status}')
