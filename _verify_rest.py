import requests, json

url = 'https://envalior-sb.eu-1.celonis.cloud'
token = 'NTFkY2QyZTQtMDQ4OS00MTljLThhMGUtMWJkMGRlOTUxNTkzOk5KM0RITkh2eVJvZmt6Z0ZvU05tT0s2MHRwMnc5cGhtaGw1a1l3L0tkTFAx'
pool_id = '663e7e2d-74f0-4cf1-a0b4-ca4faf5fee06'
headers = {'Authorization': 'AppKey ' + token}

# Get table info
r = requests.get(
    url + '/integration/api/pools/' + pool_id + '/tables',
    headers=headers
)
print("Status:", r.status_code)

if r.status_code == 200:
    tables = r.json()
    for t in tables:
        name = t.get("tableName", t.get("name", "?"))
        if "PO_EXTRACTION" in name.upper():
            print("\n=== FOUND TABLE ===")
            print(json.dumps(t, indent=2, default=str)[:2000])
else:
    print("Error:", r.text[:500])
