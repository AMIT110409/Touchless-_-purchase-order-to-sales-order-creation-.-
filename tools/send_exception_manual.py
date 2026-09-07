import os, sys, io, requests
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')
from dotenv import load_dotenv
load_dotenv()
from pathlib import Path
from graph_email_handler import GraphEmailHandler
from azure_email_tracker import AzureEmailTracker

MSG_ID  = "AAMkAGEzNTJjNTY1LTAxZjctNGU1My04ZDFkLTBkMTJlZGE0YTUzMwBGAAAAAAC7qJY7ZJJBT5IP79n-R0j-BwCJ1AznZ0-uRrG_5NNdUHgAAABL8we5AACJ1AznZ0-uRrG_5NNdUHgAAACYdMs_AAA="
SUBJECT = "FW: NY24409 - Amendments to Order SO#1568613 - Christian 31"

failure_reason = (
    "Email body could not be parsed as a Purchase Order — no PO line items found.\n\n"
    "This appears to be a delivery schedule amendment referencing existing order "
    "NY24409 / SO#1568613 (WHS Plastics — 10,000 units on 16/11/2026 marked 'New Order').\n\n"
    "The email body contains a delivery table but NO PDF Purchase Order was attached, "
    "so the agent could not automatically create a Sales Order.\n\n"
    "Requested action:\n"
    "  • If this is a new delivery line on SO#1568613 — please update SAP manually.\n"
    "  • If this is a completely new order — please ask WHS Plastics to resend with a PDF PO attached."
)

handler = GraphEmailHandler()
token   = handler._get_token()
mailbox = handler.mailbox

print(f"Mailbox   : {mailbox}")
print(f"Message ID: {MSG_ID[:50]}...")

# Fetch original email as .msg from Graph API
script_dir = Path(__file__).parent
eml_path = str(script_dir / "_ny24409_original.msg")
print(f"\nFetching original .msg from Graph API...")
resp = requests.get(
    f"https://graph.microsoft.com/v1.0/users/{mailbox}/messages/{MSG_ID}/$value",
    headers={"Authorization": f"Bearer {token}", "Accept": "application/octet-stream"},
    timeout=30,
)
att_files = []
if resp.status_code == 200:
    with open(eml_path, "wb") as f:
        f.write(resp.content)
    att_files = [eml_path]
    print(f"[OK] Fetched original email ({len(resp.content)//1024} KB) -> {eml_path}")
else:
    print(f"[WARN] Graph API {resp.status_code}: {resp.text[:200]}")
    print("       Sending exception WITHOUT email attachment...")

# Send the exception email
print(f"\nSending exception email...")
ok = handler.send_exception_email(
    message_id        = MSG_ID,
    failure_reason    = failure_reason,
    sales_org         = None,
    original_filename = SUBJECT,
    file_path         = att_files,
    dry_run           = False,
    header_fields     = {},
    status            = "EXTRACTION_FAILED",
    missing_materials = [],
)

if ok:
    print(f"\n[OK] Exception email sent for NY24409!")
    # Update Azure tracker
    tracker = AzureEmailTracker()
    tracker.update_status(MSG_ID, "EXCEPTION_ROUTED")
    print(f"[OK] Azure tracker updated: FOLDER_MOVED -> EXCEPTION_ROUTED")
    # Clean up
    if os.path.exists(eml_path):
        os.remove(eml_path)
else:
    print("[ERROR] Failed to send exception email.")
    sys.exit(1)
