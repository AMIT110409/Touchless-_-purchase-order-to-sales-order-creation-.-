"""
Diagnose Graph API Permissions
Checks what permissions the token has and tests basic mailbox access.
"""
import os, sys, json
os.environ['PYTHONIOENCODING'] = 'utf-8'
if sys.platform == "win32":
    import codecs
    sys.stdout = codecs.getwriter("utf-8")(sys.stdout.detach())
    sys.stderr = codecs.getwriter("utf-8")(sys.stderr.detach())

import msal
import requests
from dotenv import load_dotenv
load_dotenv()

client_id = os.getenv("MS_GRAPH_CLIENT_ID")
tenant_id = os.getenv("MS_GRAPH_TENANT_ID")
client_secret = os.getenv("MS_GRAPH_CLIENT_SECRET")
mailbox = os.getenv("TARGET_EMAIL_USER", "salesorders@envalior.com")

print(f"Client ID:  {client_id[:12]}...")
print(f"Tenant ID:  {tenant_id[:12]}...")
print(f"Mailbox:    {mailbox}")
print()

# 1. Acquire token
app = msal.ConfidentialClientApplication(
    client_id,
    authority=f"https://login.microsoftonline.com/{tenant_id}",
    client_credential=client_secret,
)
result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])

if "access_token" not in result:
    print(f"FAILED to get token: {result.get('error_description', result)}")
    sys.exit(1)

token = result["access_token"]
print("Token acquired successfully")

# Decode token to check roles (JWT middle part)
import base64
parts = token.split(".")
# Add padding
padded = parts[1] + "=" * (4 - len(parts[1]) % 4)
payload = json.loads(base64.b64decode(padded))
roles = payload.get("roles", [])
print(f"\nGranted API Permissions (roles):")
for r in roles:
    print(f"  - {r}")

headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

# 2. Test: Read mailbox (Mail.ReadWrite)
print(f"\n--- Test: Read mailbox messages ---")
resp = requests.get(
    f"https://graph.microsoft.com/v1.0/users/{mailbox}/messages?$top=1&$select=subject",
    headers=headers
)
print(f"  Status: {resp.status_code}")
if resp.status_code == 200:
    msgs = resp.json().get("value", [])
    if msgs:
        print(f"  Latest: {msgs[0].get('subject', 'N/A')[:60]}")
    print("  Mail.ReadWrite: OK")
else:
    print(f"  Error: {resp.text[:200]}")

# 3. Test: Send mail (Mail.Send)
print(f"\n--- Test: Send mail permission ---")
resp2 = requests.post(
    f"https://graph.microsoft.com/v1.0/users/{mailbox}/sendMail",
    headers=headers,
    json={
        "message": {
            "subject": "[TEST] Graph API Permission Check",
            "body": {"contentType": "Text", "content": "This is an automated permission test. Please ignore."},
            "toRecipients": [{"emailAddress": {"address": mailbox}}],
        },
        "saveToSentItems": "false",
    }
)
print(f"  Status: {resp2.status_code}")
if resp2.status_code == 202:
    print("  Mail.Send: OK")
else:
    print(f"  Error: {resp2.text[:300]}")
    if resp2.status_code == 403:
        print("\n  >>> MISSING PERMISSION: Mail.Send (Application)")
        print("  >>> Go to Azure Portal > App Registrations > API Permissions")
        print("  >>> Add: Microsoft Graph > Application > Mail.Send")
        print("  >>> Click 'Grant admin consent'")
