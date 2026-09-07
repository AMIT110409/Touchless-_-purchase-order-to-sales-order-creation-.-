"""
resend_robona_gewiss_933.py
===========================
Re-sends the Robona notification for Gewiss PO 933/26 (SO# 0001536505)
using the CORRECT message_id and conversation_id (envalior - ord. 933.26.pdf).

Problem: The previous send used a record with no conversation_id, so the
original customer email was NOT attached. This script corrects that.
"""

import sys, os
os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
from dotenv import load_dotenv
load_dotenv()

from run_celonis_feedback import (
    get_graph_token,
    fetch_thread_eml_attachments,
    _fetch_single_eml,
    download_po_pdf,
    send_email_via_graph,
)
from email_templates import get_robona_template

# ─── Configuration ────────────────────────────────────────────────────────────
mailbox         = os.getenv("TARGET_EMAIL_USER", "salesorders@envalior.com")
robona_mailbox  = os.getenv("ROBONA_SERVICE_MAILBOX", "robona-test@envalior.com")
test_email      = os.getenv("TEST_CS_EMAIL", "a.rathore@ofiservices.com")

# ─── Correct record for Gewiss PO 933/26 ─────────────────────────────────────
# This is the FW: Christian 18 record which has the real conversation_id
MSG_ID          = "AAMkAGEzNTJjNTY1LTAxZjctNGU1My04ZDFkLTBkMTJlZGE0YTUzMwBGAAAAAAC7qJY7ZJJBT5IP79n-R0j-BwCJ1AznZ0-uRrG_5NNdUHgAAABL8we5AACJ1AznZ0-uRrG_5NNdUHgAAACD7hB0AAA="
CONVERSATION_ID = "AAQkAGEzNTJjNTY1LTAxZjctNGU1My04ZDFkLTBkMTJlZGE0YTUzMwAQACef"
SOURCE_FILE     = "envalior - ord. 933.26_aamkagez.pdf"
SUBJECT         = "FW: Christian 18 - Customer in Sales orgs 2540 and 2545, PO in IT language"
SO_NUMBER       = "0001536505"
PO_NUMBER       = "933/26"

DRY_RUN = "--dry-run" in sys.argv

print("=" * 70)
print("  RE-SENDING ROBONA FOR GEWISS PO 933/26  |  SO# 0001536505")
print(f"  Mode: {'DRY-RUN (no real emails)' if DRY_RUN else 'LIVE SEND'}")
print("=" * 70)

# 1. Get Graph token
graph_token = get_graph_token()
print(f"\n  Graph token acquired: {bool(graph_token)}")
if not graph_token and not DRY_RUN:
    print("  [ERROR] Cannot send without Graph API token. Check .env credentials.")
    sys.exit(1)

# 2. Download PO PDF from Azure Blob
print(f"\n  [PDF] Downloading PDF: {SOURCE_FILE}")
pdf_bytes = download_po_pdf(SOURCE_FILE, dry_run=DRY_RUN)

if not pdf_bytes and not DRY_RUN:
    # Fallback: fetch from Graph email attachment directly
    print(f"  [PDF] Blob failed — trying Graph API attachment fallback for msg_id={MSG_ID[:25]}...")
    from run_celonis_feedback import fetch_graph_message_pdf_attachment
    pdf_bytes, pdf_name_from_graph = fetch_graph_message_pdf_attachment(graph_token, mailbox, MSG_ID)
    if pdf_bytes:
        print(f"  [PDF] Retrieved from Graph API attachment: {len(pdf_bytes):,} bytes")

pdf_name = SOURCE_FILE if SOURCE_FILE.endswith(".pdf") else f"PO_{PO_NUMBER.replace('/', '_')}.pdf"
print(f"  [PDF] Status: {'YES (' + str(len(pdf_bytes)) + ' bytes)' if pdf_bytes else 'NOT FOUND — will send without PDF'}")

# 3. Fetch original email thread as .msg attachment
thread_atts = []
if not DRY_RUN:
    # Primary: fetch by exact message_id (the _aamkagez forward email)
    print(f"\n  [MSG] Fetching original email as .msg (msg_id={MSG_ID[:25]}...)...")
    thread_atts = _fetch_single_eml(graph_token, mailbox, MSG_ID, SUBJECT)

    if not thread_atts:
        # Fallback: fetch full conversation thread
        print(f"  [CHAIN] Single .msg failed — fetching full thread via conversationId...")
        thread_atts = fetch_thread_eml_attachments(
            token=graph_token,
            mailbox=mailbox,
            conversation_id=CONVERSATION_ID,
        )
    print(f"  [MSG] Email attachments: {len(thread_atts)} .msg file(s) attached")
else:
    print(f"\n  [DRY-RUN] Would attach .msg for msg_id={MSG_ID[:25]}...")
    print(f"  [DRY-RUN] Would attach thread for conversationId={CONVERSATION_ID[:25]}...")

# 4. Build email
robona_subject = f"{SUBJECT} SO# {SO_NUMBER}"
robona_body    = get_robona_template(SO_NUMBER, MSG_ID, SOURCE_FILE, mailbox)

print(f"\n  [SEND] To      : {robona_mailbox}")
print(f"  [SEND] CC      : {test_email}")
print(f"  [SEND] Subject : {robona_subject[:80]}")
print(f"  [SEND] PDF     : {'YES (' + str(len(pdf_bytes or b'')) + ' bytes)' if pdf_bytes else 'NO'}")
print(f"  [SEND] .msg    : {len(thread_atts)} attachment(s)")

# 5. Send
ok = send_email_via_graph(
    graph_token, mailbox, robona_mailbox,
    robona_subject, robona_body,
    dry_run=DRY_RUN,
    pdf_bytes=pdf_bytes,
    pdf_filename=pdf_name,
    extra_attachments=thread_atts or None,
    cc_address=test_email,
)

print("\n" + "=" * 70)
if ok:
    print(f"  ✅ SUCCESS — Robona re-sent for PO {PO_NUMBER} / SO# {SO_NUMBER}")
    if not DRY_RUN:
        # Update tracker status
        from azure_email_tracker import AzureEmailTracker
        tracker = AzureEmailTracker()
        tracker.update_status(MSG_ID, 'ROBONA_SENT', extra_fields={'so_number': SO_NUMBER})
        print(f"  ✅ Tracker updated: ROBONA_SENT for msg_id={MSG_ID[:25]}...")
else:
    print(f"  ❌ FAILED — Check Graph API credentials and mailbox permissions")
print("=" * 70)
