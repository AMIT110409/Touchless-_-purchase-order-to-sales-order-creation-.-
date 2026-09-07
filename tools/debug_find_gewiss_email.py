"""
debug_find_gewiss_email.py
Find the original 'new po' Gewiss email across ALL Outlook folders
and resend Robona with it attached as .msg
"""
import os, requests, base64, sys
os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
from dotenv import load_dotenv; load_dotenv()
from run_celonis_feedback import get_graph_token, _fetch_single_eml, send_email_via_graph
from email_templates import get_robona_template
from azure_email_tracker import AzureEmailTracker

token   = get_graph_token()
mailbox = os.getenv("TARGET_EMAIL_USER", "salesorders@envalior.com")
robona_mailbox = os.getenv("ROBONA_SERVICE_MAILBOX", "robona-test@envalior.com")
test_email = os.getenv("TEST_CS_EMAIL", "a.rathore@ofiservices.com")
headers = {"Authorization": f"Bearer {token}"}

SO_NUMBER  = "0001536505"
PO_NUMBER  = "933/26"

# Get full conversation_ids for 'new po' / 516 records from tracker
tracker = AzureEmailTracker()
all_emails = tracker.get_eligible_emails([
    'PUSHED_TO_CELONIS','MAPPED_SUCCESS','ROBONA_SENT','PENDING_STAGE2','FOLDER_MOVED'
])

print("=== Searching ALL folders for original 'new po' Gewiss email ===\n")

found_msg_id    = None
found_subject   = None
found_conv_id   = None
best_eml_bytes  = None

for e in all_emails:
    src   = str(e.get('source_file','') or '').lower()
    subj  = str(e.get('subject','')    or '').lower()
    conv  = e.get('conversation_id', '') or ''
    msg_id = e.get('message_id', '') or ''

    if not conv:
        continue
    # Target: new po emails with 516 or oa logital source
    if not ('new po' in subj or '516' in src or 'order' in subj.lower()):
        continue
    if not ('933' in src or '516' in src or 'order' in src or 'new po' in subj):
        continue

    print(f"  Checking: subj='{e.get('subject')}' | src='{e.get('source_file')}' | conv={conv[:40]}...")

    # Search ALL folders using conversationId
    url = (
        f"https://graph.microsoft.com/v1.0/users/{mailbox}/messages"
        f"?$filter=conversationId eq '{conv}'"
        f"&$select=id,subject,from,receivedDateTime,parentFolderId"
        f"&$top=10"
    )
    r = requests.get(url, headers=headers, timeout=30)
    if r.status_code != 200:
        print(f"    Graph returned {r.status_code}: {r.text[:80]}")
        continue

    msgs = r.json().get("value", [])
    print(f"    Found {len(msgs)} message(s) in ALL folders")

    for m in msgs:
        m_id   = m.get("id", "")
        m_subj = m.get("subject", "")
        m_from = m.get("from", {}).get("emailAddress", {}).get("address", "")
        m_recv = m.get("receivedDateTime", "")[:10]
        print(f"      msg: subj={m_subj[:50]} | from={m_from} | date={m_recv}")

        # Try to download as .eml
        eml_url = f"https://graph.microsoft.com/v1.0/users/{mailbox}/messages/{m_id}/$value"
        eml_r   = requests.get(eml_url, headers={**headers, "Accept": "application/octet-stream"}, timeout=30)
        if eml_r.status_code == 200:
            print(f"      ✅ Downloaded {len(eml_r.content):,} bytes as .eml")
            # Pick the original customer email (from outside @envalior.com)
            if best_eml_bytes is None or ('envalior' not in m_from.lower()):
                best_eml_bytes  = eml_r.content
                found_msg_id    = m_id
                found_subject   = m_subj
                found_conv_id   = conv
        else:
            print(f"      ❌ .eml download failed: {eml_r.status_code}")

if best_eml_bytes:
    print(f"\n✅ Found original email: '{found_subject}' ({len(best_eml_bytes):,} bytes)")
    print(f"   msg_id: {found_msg_id[:50]}...")

    # Build .msg attachment
    safe_subj = (found_subject or "original_email")[:50].replace("/", "_").replace("\\", "_")
    eml_att = [{
        "@odata.type":  "#microsoft.graph.fileAttachment",
        "name":         f"original_{safe_subj}.msg",
        "contentType":  "message/rfc822",
        "contentBytes": base64.b64encode(best_eml_bytes).decode("utf-8"),
    }]

    # Also get the PDF
    pdf_local = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "outlook_po_archive",
        "envalior - ord. 933.26_aamkagez.pdf"
    )
    pdf_bytes = open(pdf_local, "rb").read() if os.path.exists(pdf_local) else None
    pdf_name  = "envalior - ord. 933.26_aamkagez.pdf"
    if pdf_bytes:
        print(f"   PDF: {len(pdf_bytes):,} bytes from local archive")

    robona_subject = f"FW: Christian 18 - Customer in Sales orgs 2540 and 2545, PO in IT language SO# {SO_NUMBER}"
    robona_body    = get_robona_template(SO_NUMBER, found_msg_id, pdf_name, mailbox)

    print(f"\n  Sending Robona with .msg + PDF to {robona_mailbox}...")
    DRY_RUN = "--dry-run" in sys.argv
    ok = send_email_via_graph(
        token, mailbox, robona_mailbox,
        robona_subject, robona_body,
        dry_run=DRY_RUN,
        pdf_bytes=pdf_bytes,
        pdf_filename=pdf_name,
        extra_attachments=eml_att,
        cc_address=test_email,
    )
    if ok and not DRY_RUN:
        tracker.update_status(found_msg_id, 'ROBONA_SENT', extra_fields={'so_number': SO_NUMBER})
        print("  ✅ Tracker updated: ROBONA_SENT")
    print("  ✅ Done!" if ok else "  ❌ Send failed")
else:
    print("\n❌ Could not find the original email in any folder.")
    print("   The email may have been permanently deleted or is in a folder without Graph access.")
    print("   Please MANUALLY forward the original 'new po' email to:")
    print(f"   To  : {robona_mailbox}")
    print(f"   Subj: FW: ... SO# {SO_NUMBER}")
