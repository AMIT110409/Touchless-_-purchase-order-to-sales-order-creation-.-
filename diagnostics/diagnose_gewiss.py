"""
diagnose_gewiss.py
Properly identifies:
1. The FULL message_id from Azure for the FOLDER_MOVED Gewiss email
2. The correct original email (from Christian Burwinkel, NOT the sent Robona copy)
3. Whether the local PDF archive file is correct
"""
import os, sys, requests, fitz
os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
from dotenv import load_dotenv; load_dotenv()
from run_celonis_feedback import get_graph_token
from azure_email_tracker import AzureEmailTracker

token   = get_graph_token()
mailbox = os.getenv("TARGET_EMAIL_USER", "salesorders@envalior.com")
headers = {"Authorization": f"Bearer {token}"}

print("=" * 70)
print("  GEWISS 933/26 — FULL DIAGNOSTIC")
print("=" * 70)

# ── 1. Get FULL tracker record for Gewiss FOLDER_MOVED email ──────────────
print("\n[1] Pulling FULL record from Azure Tracker (no truncation)...")
tracker = AzureEmailTracker()
all_emails = tracker.get_eligible_emails([
    'PUSHED_TO_CELONIS','MAPPED_SUCCESS','ROBONA_SENT','PENDING_STAGE2','FOLDER_MOVED'
])

gewiss_records = []
for e in all_emails:
    src  = str(e.get('source_file','') or '').lower()
    subj = str(e.get('subject','') or '').lower()
    if '933' in src or 'christian 18' in subj or 'ord. 933' in src or '516' in src:
        gewiss_records.append(e)

print(f"\n  Found {len(gewiss_records)} Gewiss-related record(s):")
for e in gewiss_records:
    print(f"\n  --- record ---")
    print(f"  subject     : {e.get('subject','')}")
    print(f"  source_file : {e.get('source_file','')}")
    print(f"  status      : {e.get('status','')}")
    print(f"  sender      : {e.get('sender_email','')}")
    print(f"  msg_id FULL : {e.get('message_id','')}")
    print(f"  conv_id FULL: {e.get('conversation_id','')}")

# ── 2. Check actual PDF content in the local archive ─────────────────────
print("\n\n[2] Checking local PDF archive file...")
pdf_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "outlook_po_archive", "envalior - ord. 933.26_aamkagez.pdf")
if os.path.exists(pdf_path):
    doc = fitz.open(pdf_path)
    text = ""
    for page in doc:
        text += page.get_text()
    doc.close()
    print(f"  File: {pdf_path}")
    print(f"  Size: {os.path.getsize(pdf_path):,} bytes")
    print(f"  First 600 chars of text:")
    print("  " + text[:600].replace("\n", "\n  "))
else:
    print(f"  NOT FOUND: {pdf_path}")

# ── 3. Search for the ORIGINAL incoming email from Christian Burwinkel ────
print("\n\n[3] Searching for original email from Christian.Burwinkel@Envalior.com...")
url = (
    f"https://graph.microsoft.com/v1.0/users/{mailbox}/messages"
    f"?$search=\"Christian 18\""
    f"&$select=id,subject,from,receivedDateTime,parentFolderId"
    f"&$top=25"
)
r = requests.get(url, headers=headers, timeout=30)
if r.status_code == 200:
    msgs = r.json().get("value", [])
    print(f"  Found {len(msgs)} message(s) matching 'Christian 18':")
    for m in msgs:
        m_from = m.get("from", {}).get("emailAddress", {}).get("address", "")
        m_subj = m.get("subject", "")
        m_recv = m.get("receivedDateTime", "")[:16]
        m_id   = m.get("id", "")
        print(f"\n    subject : {m_subj[:70]}")
        print(f"    from    : {m_from}")
        print(f"    date    : {m_recv}")
        print(f"    msg_id  : {m_id[:60]}...")
else:
    print(f"  Search failed: {r.status_code}")
