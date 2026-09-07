"""Reload the Celonis Data Model to make PO_EXTRACTION_RESULTS visible."""
from pycelonis import get_celonis

url = 'https://envalior-sb.eu-1.celonis.cloud/'
token = 'NTFkY2QyZTQtMDQ4OS00MTljLThhMGUtMWJkMGRlOTUxNTkzOk5KM0RITkh2eVJvZmt6Z0ZvU05tT0s2MHRwMnc5cGhtaGw1a1l3L0tkTFAx'

c = get_celonis(url, token)
pool = c.data_integration.get_data_pool('663e7e2d-74f0-4cf1-a0b4-ca4faf5fee06')
dm = pool.get_data_model('3f193f92-a398-4989-915c-cc100e67d421')

print("Data Model:", dm.name if hasattr(dm, 'name') else dm)
print("\nReloading Data Model...")
try:
    dm.reload()
    print("SUCCESS: Data Model reloaded.")
except Exception as e:
    print(f"Reload error: {e}")
    print("\nTrying force reload via API...")
    import requests
    headers = {'Authorization': 'AppKey ' + token}
    r = requests.post(
        url + 'integration/api/pools/663e7e2d-74f0-4cf1-a0b4-ca4faf5fee06/data-models/3f193f92-a398-4989-915c-cc100e67d421/reload',
        headers=headers
    )
    print(f"API reload status: {r.status_code}")
    print(r.text[:500] if r.text else "OK")

# List tables in data model
print("\nTables in Data Model:")
tables = dm.get_tables()
for t in tables:
    n = t.name if hasattr(t, 'name') else str(t)
    if 'extraction' in n.lower() or 'po_' in n.lower():
        print(f"  *** {n} ***")
    else:
        print(f"  - {n}")
