"""
resend_robona_gewiss_correct.py
================================
Re-sends Robona for Gewiss PO 933/26 with the CORRECT .msg attachment:
  - PDF: envalior - ord. 933.26_aamkagez.pdf (163 KB, the actual Gewiss PO)
  - .msg: The "Christian 18" email that originally brought in the Gewiss PO
            (NOT the unrelated "new po" / ORDER(1)(1).PDF email)
"""
import os, sys, requests, base64
os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
from dotenv import load_dotenv; load_dotenv()

from run_celonis_feedback import get_graph_token, send_email_via_graph
from email_templates import get_robona_template
from azure_email_tracker import AzureEmailTracker

mailbox        = os.getenv("TARGET_EMAIL_USER", "salesorders@envalior.com")
robona_mailbox = os.getenv("ROBONA_SERVICE_MAILBOX", "robona-test@envalior.com")
test_email     = os.getenv("TEST_CS_EMAIL", "a.rathore@ofiservices.com")
DRY_RUN        = "--dry-run" in sys.argv

SO_NUMBER  = "0001536505"
PO_NUMBER  = "933/26"

# ── These are the CORRECT identifiers for the Gewiss "Christian 18" email ──
# source: envalior - ord. 933.26_aamkagez.pdf | status: PENDING_STAGE2
GEWISS_MSG_ID   = "AAMkAGEzNTJjNTY1LTAxZjctNGU1My04ZDFkLTBkMTJlZGE0YTUzMwBGAAAAAAC7qJY7ZJJBT5IP79n-R0j-BwCJ1AznZ0-uRrG_5NNdUHgAAABL8we5AACJ1AznZ0-uRrG_5NNdUHgAAACD7hB0AAA="
GEWISS_SUBJECT  = "Christian 18 - Customer in Sales orgs 2540 and 2545, PO in IT language"
PDF_FILENAME    = "envalior - ord. 933.26_aamkagez.pdf"

print("=" * 70)
print("  RE-SENDING ROBONA (CORRECTED) — Gewiss PO 933/26 | SO# 0001536505")
print(f"  Mode: {'DRY-RUN' if DRY_RUN else 'LIVE SEND'}")
print("=" * 70)

token = get_graph_token()
print(f"\n  Graph token: {bool(token)}")
headers = {"Authorization": f"Bearer {token}"}

# ── Step 1: Load correct PDF from local archive ───────────────────────────
pdf_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "outlook_po_archive", PDF_FILENAME)
if os.path.exists(pdf_path):
    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()
    print(f"\n  [PDF] Loaded: {PDF_FILENAME} ({len(pdf_bytes):,} bytes) ✅")
else:
    pdf_bytes = None
    print(f"\n  [PDF] Not found locally: {pdf_path}")

# ── Step 2: Fetch the CORRECT email (.msg) by message_id across all folders ──
# This is the "Christian 18" email with Gewiss PO attached — NOT the "new po" email
print(f"\n  [MSG] Searching ALL folders for message_id=")
print(f"        {GEWISS_MSG_ID[:60]}...")

eml_bytes  = None
eml_name   = None

if not DRY_RUN:
    # Try direct fetch by message_id first
    eml_url = f"https://graph.microsoft.com/v1.0/users/{mailbox}/messages/{GEWISS_MSG_ID}/$value"
    eml_r   = requests.get(eml_url,
                           headers={**headers, "Accept": "application/octet-stream"},
                           timeout=30)
    if eml_r.status_code == 200:
        eml_bytes = eml_r.content
        eml_name  = f"original_{GEWISS_SUBJECT[:50].replace('/', '_')}.msg"
        print(f"  [MSG] ✅ Downloaded by message_id: {len(eml_bytes):,} bytes")
    else:
        print(f"  [MSG] message_id fetch returned {eml_r.status_code} — searching by subject...")

        # Fallback: search ALL messages by subject keyword
        url = (
            f"https://graph.microsoft.com/v1.0/users/{mailbox}/messages"
            f"?$search=\"Christian 18\""
            f"&$select=id,subject,from,receivedDateTime"
            f"&$top=10"
        )
        r = requests.get(url, headers=headers, timeout=30)
        if r.status_code == 200:
            msgs = r.json().get("value", [])
            print(f"  [MSG] Subject search found {len(msgs)} message(s)")
            for m in msgs:
                m_id   = m.get("id", "")
                m_subj = m.get("subject", "")
                m_from = m.get("from", {}).get("emailAddress", {}).get("address", "")
                m_recv = m.get("receivedDateTime", "")[:10]
                print(f"    - {m_subj[:60]} | from={m_from} | date={m_recv}")

                # Download as .eml
                eml2_url = f"https://graph.microsoft.com/v1.0/users/{mailbox}/messages/{m_id}/$value"
                eml2_r   = requests.get(eml2_url,
                                        headers={**headers, "Accept": "application/octet-stream"},
                                        timeout=30)
                if eml2_r.status_code == 200:
                    eml_bytes = eml2_r.content
                    eml_name  = f"original_{m_subj[:50].replace('/', '_')}.msg"
                    print(f"    ✅ Downloaded: {len(eml_bytes):,} bytes")
                    break
        else:
            print(f"  [MSG] Subject search failed: {r.status_code}: {r.text[:100]}")

# ── Step 3: Build .msg attachment ─────────────────────────────────────────
eml_att = []
if eml_bytes:
    eml_att = [{
        "@odata.type":  "#microsoft.graph.fileAttachment",
        "name":         eml_name or "original_gewiss_933.msg",
        "contentType":  "message/rfc822",
        "contentBytes": base64.b64encode(eml_bytes).decode("utf-8"),
    }]
    print(f"\n  [MSG] Attachment ready: {eml_name} ({len(eml_bytes):,} bytes) ✅")
else:
    print("\n  [MSG] ⚠️ Could not fetch original email — sending PDF only")

# ── Step 4: Send ──────────────────────────────────────────────────────────
robona_subject = f"{GEWISS_SUBJECT} SO# {SO_NUMBER}"
robona_body    = get_robona_template(SO_NUMBER, GEWISS_MSG_ID, PDF_FILENAME, mailbox)

print(f"\n  [SEND] To     : {robona_mailbox}")
print(f"  [SEND] CC     : {test_email}")
print(f"  [SEND] Subject: {robona_subject[:80]}")
print(f"  [SEND] PDF    : {'YES ' + str(len(pdf_bytes or b'')) + ' bytes' if pdf_bytes else 'NO'}")
print(f"  [SEND] .msg   : {len(eml_att)} attachment(s)")

ok = send_email_via_graph(
    token, mailbox, robona_mailbox,
    robona_subject, robona_body,
    dry_run=DRY_RUN,
    pdf_bytes=pdf_bytes,
    pdf_filename=PDF_FILENAME,
    extra_attachments=eml_att or None,
    cc_address=test_email,
)

print("\n" + "=" * 70)
if ok:
    print(f"  ✅ Robona re-sent CORRECTLY for Gewiss PO {PO_NUMBER} / SO# {SO_NUMBER}")
    if not DRY_RUN:
        tracker = AzureEmailTracker()
        tracker.update_status(GEWISS_MSG_ID, 'ROBONA_SENT',
                              extra_fields={'so_number': SO_NUMBER})
        print(f"  ✅ Tracker updated: ROBONA_SENT")
else:
    print(f"  ❌ Send failed — check Graph API credentials")
print("=" * 70)
