"""
move_skip_emails.py
-------------------
One-shot script to move all emails with status=SKIP from Unprocessed POs
into Processed POs and mark them FOLDER_MOVED in the DB.
Run this directly without needing the full pipeline.
"""
import sqlite3
from pathlib import Path

from setup_mailbox_folders import get_token, mark_as_processed

db_path = Path("processed_emails.db")
if not db_path.exists():
    print("ERROR: processed_emails.db not found.")
    exit(1)

def _is_valid_outlook_id(mid: str) -> bool:
    if not mid:
        return False
    m = mid.strip()
    return (m.startswith("AAMk") or m.startswith("AQMk")) and len(m) > 40

token = get_token()

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

cur.execute(
    "SELECT message_id, subject, source_file FROM processed_emails "
    "WHERE status = 'SKIP'"
)
rows = cur.fetchall()
print(f"Found {len(rows)} SKIP email(s) to move to Processed POs.\n")

moved = 0
already = 0
no_id = 0

for row in rows:
    mid = row["message_id"]
    subj = row["subject"] or ""
    label = row["source_file"] or subj[:40] or mid[:30]

    if not _is_valid_outlook_id(mid):
        print(f"  [NO_OUTLOOK_ID] Skipping (not an Outlook message ID): {repr(label)}")
        cur.execute(
            "UPDATE processed_emails SET status='NO_EMAIL_ID', "
            "updated_at=CURRENT_TIMESTAMP WHERE message_id=?",
            (mid,)
        )
        no_id += 1
        continue

    print(f"  Moving: {repr(subj[:60])} ...")
    ok = mark_as_processed(token, mid, silent=False)
    if ok:
        moved += 1
        print(f"    -> MOVED to Processed POs")
    else:
        already += 1
        print(f"    -> Already moved (404) - marking as done")

    cur.execute(
        "UPDATE processed_emails SET status='FOLDER_MOVED', "
        "updated_at=CURRENT_TIMESTAMP WHERE message_id=?",
        (mid,)
    )

conn.commit()
conn.close()

print(f"\n=== DONE ===")
print(f"  Moved to Processed POs : {moved}")
print(f"  Already moved (done)   : {already}")
print(f"  No Outlook ID          : {no_id}")
