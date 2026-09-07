"""Test MCAM alias fix in fuzzy_match_vendor."""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from reenrich_results import fuzzy_match_vendor, _load_vendor_cache

# Test the alias matching for MCAM
test_names = [
    'Mitsubishi Chemical Advanced Materials B.V.',
    'Mitsubishi Chemical Advanced Materials BV',
    'MCAM B.V.',
    'Mitsubishi Chemical Advanced Materials',
]

line_items = [{'material_code': '06030000976', 'material_description': '', 'quantity': '12600', 'unit': 'KG'}]

for name in test_names:
    result = fuzzy_match_vendor(name, line_items=line_items, address_hint='7602 PK Almelo Netherlands')
    if result:
        matched_id = result['id']
        is_correct = '4020042625' in matched_id
        icon = '[OK]' if is_correct else '[WRONG]'
        print(f'{icon} Match for "{name}": id={matched_id}  name={result["name"]}  score={result["score"]}')
    else:
        print(f'[FAIL] NO MATCH for "{name}"')
