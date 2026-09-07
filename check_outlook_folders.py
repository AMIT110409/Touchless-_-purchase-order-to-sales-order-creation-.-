"""
Diagnostic: Check actual Microsoft Graph API mailboxes and folders.
Lists messages in 'Inbox', 'Unprocessed POs', 'Processed POs', 'Exception POs'.
"""
import sys, os
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
os.environ['DISABLE_MODEL_SOURCE_CHECK'] = 'True'
from dotenv import load_dotenv
load_dotenv()

from setup_mailbox_folders import get_token, get_folder_ids, get_messages_in_folder, get_inbox_messages

token = get_token()
if not token:
    print("ERROR: Could not get Graph API token")
    exit(1)

folders = get_folder_ids(token)
print("=" * 70)
print("  Graph API Mailbox Folders Diagnostic")
print("=" * 70)

print("\n--- Inbox Messages ---")
inbox_msgs = get_inbox_messages(token, top=20)
print(f"Total Inbox Messages: {len(inbox_msgs)}")
for m in inbox_msgs:
    mid  = m.get("id", "")
    subj = m.get("subject", "")
    recv = m.get("receivedDateTime", "")
    att  = m.get("hasAttachments", False)
    print(f"  [{recv[:19]}] ID: {mid[:35]}... | Att: {att} | Subject: {subj}")

for fname, fid in folders.items():
    msgs = get_messages_in_folder(token, fid, top=50)
    print(f"\n--- Folder '{fname}' (ID: {fid[:20]}...) — {len(msgs)} messages ---")
    for m in msgs:
        mid  = m.get("id", "")
        subj = m.get("subject", "")
        recv = m.get("receivedDateTime", "")
        att  = m.get("hasAttachments", False)
        print(f"  [{recv[:19]}] ID: {mid[:35]}... | Att: {att} | Subject: {subj}")
