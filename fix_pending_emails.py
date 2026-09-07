"""
fix_pending_emails.py
--------------------
One-time fix: marks UAT test emails as SKIP so they stop recycling
through the Unprocessed POs folder every pipeline run.

Also shows a summary of which PENDING emails remain (if any) after the fix,
so you know if there are real POs that need attention.
"""
import sqlite3
import sys

conn = sqlite3.connect("processed_emails.db")
cur = conn.cursor()

# --- Fix 1: Mark UAT test emails as SKIP ---
# UAT ASOS Scenario 2 and 3 are internal test emails with no real PO attachment.
# They have been cycling through Unprocessed POs indefinitely because:
#   - status stays PENDING (no JSONL output was generated for them)
#   - PENDING is not in FINAL_STATUSES, so they are re-polled every run
cur.execute(
    "UPDATE processed_emails SET status='SKIP', updated_at=CURRENT_TIMESTAMP "
    "WHERE status='PENDING' AND (subject LIKE 'UAT%' OR subject LIKE 'Test%' OR subject LIKE 'FW: UAT%')"
)
uat_fixed = cur.rowcount
print(f"  Fixed {uat_fixed} UAT/test email(s) -> SKIP")

# --- Show remaining PENDING ---
cur.execute(
    "SELECT message_id, subject, source_file, attachments, updated_at "
    "FROM processed_emails WHERE status='PENDING' ORDER BY updated_at DESC"
)
remaining = cur.fetchall()
print(f"\n  Remaining PENDING emails after fix: {len(remaining)}")
for r in remaining:
    print(f"    msg={r[0][:20]}... | subject={r[1]} | file={r[2]} | updated={r[4]}")

conn.commit()
conn.close()
print("\nDone.")
