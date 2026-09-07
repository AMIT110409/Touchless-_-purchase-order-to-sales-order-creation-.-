"""
Diagnostic: verify Azure Table Storage connection and show current email status breakdown.
"""
import sys, os
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
os.environ['DISABLE_MODEL_SOURCE_CHECK'] = 'True'

from dotenv import load_dotenv
load_dotenv()

from azure_email_tracker import AzureEmailTracker

print("=" * 60)
print("  Azure Email Tracker — Connection & Status Diagnostic")
print("=" * 60)

tracker = AzureEmailTracker()

mode = "Azure Table Storage" if not tracker._sqlite_mode else "SQLite (fallback)"
print(f"\n  Backend   : {mode}")
print(f"  SQLite mode: {tracker._sqlite_mode}")
print(f"  Azure client: {'Connected' if tracker._azure_client else 'None'}")

print("\n--- Status breakdown (ALL records) ---")
ALL_STATUSES = [
    "PENDING", "MAPPED_SUCCESS", "PUSHED_TO_CELONIS", "PENDING_STAGE2",
    "FOLDER_MOVED", "NO_EMAIL_ID", "ARCHIVED", "EXCEPTION_ROUTED",
    "MAPPING_FAILED", "EXTRACTION_FAILED", "SKIP", "SKIP_DUPLICATE",
    "ROBONA_SENT", "SO_BLOCKED", "SO_CREATION_FAILED",
]
total = 0
for status in ALL_STATUSES:
    rows = tracker.get_eligible_emails([status])
    if rows:
        print(f"  {status:<30}: {len(rows)}")
        total += len(rows)

print(f"\n  TOTAL tracked emails: {total}")

print("\n--- Most recent PENDING emails (up to 10) ---")
pending = tracker.get_eligible_emails(["PENDING"])
for r in pending[:10]:
    mid  = r.get("message_id") or r.get("RowKey", "")
    subj = (r.get("subject") or "")[:70]
    atts = (r.get("attachments") or "")[:60]
    upd  = r.get("updated_at", "")
    print(f"  msg_id: {mid[:50]}...")
    print(f"    subject: {subj}")
    print(f"    attachments: {atts}")
    print(f"    updated_at: {upd}")
    print()

print("\n--- Most recent MAPPED_SUCCESS / PUSHED_TO_CELONIS (up to 5) ---")
done = tracker.get_eligible_emails(["MAPPED_SUCCESS", "PUSHED_TO_CELONIS"])
for r in done[:5]:
    mid  = r.get("message_id") or r.get("RowKey", "")
    subj = (r.get("subject") or "")[:70]
    sf   = (r.get("source_file") or "")[:60]
    upd  = r.get("updated_at", "")
    print(f"  [{r.get('status')}] {subj}")
    print(f"    source_file: {sf}")
    print(f"    updated_at: {upd}")
    print()

print("=" * 60)
print("  Robona SO Tracker — Azure Table 'robonasentlog'")
print("=" * 60)
try:
    from robona_so_tracker import RobonaSOTracker
    r_tracker = RobonaSOTracker()
    so_set = r_tracker.get_all_sent_so_numbers()
    print(f"\n  Backend    : Azure Table Storage (robonasentlog)")
    print(f"  Total SOs  : {len(so_set)} unique Sales Order(s) recorded")
    records = r_tracker.list_all_sent()
    if records:
        print("\n--- Recent Robona Sent SO Records (up to 5) ---")
        for rec in records[:5]:
            so  = rec.get("RowKey", "")
            po  = rec.get("po_number", "")
            ts  = rec.get("sent_at", "")
            src = rec.get("source_file", "")
            print(f"  SO: {so:<15} | PO: {po:<20} | Sent: {ts} | File: {src}")
except Exception as e:
    print(f"  [WARN] Could not query RobonaSOTracker: {e}")

