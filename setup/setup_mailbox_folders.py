"""
=============================================================================
Mailbox Folder Setup + Email Mover
=============================================================================
Creates two folders in salesorders@envalior.com mailbox:
  - "Processed POs"   → emails go here after successful extraction
  - "Unprocessed POs" → emails go here if extraction failed / not yet done

Also provides helper functions used by the pipeline to move emails.

Usage:
  python setup_mailbox_folders.py              # Create folders only
  python setup_mailbox_folders.py --move-all   # Move all existing emails to Unprocessed
  python setup_mailbox_folders.py --status     # Show folder counts
=============================================================================
"""

import os
import sys
import json
import argparse
import requests
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
CLIENT_ID     = os.getenv("MS_GRAPH_CLIENT_ID")
TENANT_ID     = os.getenv("MS_GRAPH_TENANT_ID")
CLIENT_SECRET = os.getenv("MS_GRAPH_CLIENT_SECRET")
TARGET_USER   = os.getenv("TARGET_EMAIL_USER", "salesorders@envalior.com")
GRAPH_BASE    = "https://graph.microsoft.com/v1.0"

PROCESSED_FOLDER_NAME   = "Processed POs"
UNPROCESSED_FOLDER_NAME = "Unprocessed POs"
EXCEPTION_FOLDER_NAME   = "Exception POs"

# ─────────────────────────────────────────────────────────────────────────────
# Auth
# ─────────────────────────────────────────────────────────────────────────────
def get_token() -> str:
    """Get access token via client credentials (service principal)."""
    import msal
    authority = f"https://login.microsoftonline.com/{TENANT_ID}"
    app = msal.ConfidentialClientApplication(
        CLIENT_ID, authority=authority, client_credential=CLIENT_SECRET
    )
    result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" in result:
        return result["access_token"]
    raise RuntimeError(f"Token error: {result.get('error_description')}")


def headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


import time

def _request_with_retry(method: str, url: str, **kwargs):
    """Wrapper around requests with exponential backoff retry for stability."""
    for attempt in range(1, 4):
        try:
            r = requests.request(method, url, timeout=30, **kwargs)
            return r
        except (requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError,
                requests.exceptions.Timeout) as e:
            if attempt == 3:
                raise
            wait = 2 ** attempt
            print(f"  [WARN] Graph API connection error (attempt {attempt}/3): {e}. Retrying in {wait}s...")
            time.sleep(wait)

# ─────────────────────────────────────────────────────────────────────────────
# Folder Operations
# ─────────────────────────────────────────────────────────────────────────────
def list_mail_folders(token: str) -> list:
    """List all top-level mail folders."""
    url = f"{GRAPH_BASE}/users/{TARGET_USER}/mailFolders?$top=100"
    r = _request_with_retry("GET", url, headers=headers(token))
    r.raise_for_status()
    return r.json().get("value", [])


def get_folder_by_name(token: str, name: str) -> dict | None:
    """Find a folder by display name (returns None if not found)."""
    folders = list_mail_folders(token)
    for f in folders:
        if f.get("displayName", "").lower() == name.lower():
            return f
    return None


def create_folder(token: str, name: str) -> dict:
    """Create a top-level mail folder. Returns the folder object."""
    url = f"{GRAPH_BASE}/users/{TARGET_USER}/mailFolders"
    payload = {"displayName": name}
    r = _request_with_retry("POST", url, headers=headers(token), json=payload)
    r.raise_for_status()
    return r.json()


def get_or_create_folder(token: str, name: str) -> dict:
    """Get existing folder or create it if it doesn't exist."""
    folder = get_folder_by_name(token, name)
    if folder:
        print(f"  [EXISTS]  Folder '{name}' already exists  (id: {folder['id'][:20]}...)")
        return folder
    folder = create_folder(token, name)
    print(f"  [CREATED] Folder '{name}' created  (id: {folder['id'][:20]}...)")
    return folder


def move_email_to_folder(token: str, message_id: str, folder_id: str, silent: bool = False) -> bool:
    """Move a single email to a destination folder.
    
    Args:
        silent: If True, suppress the [WARN] print on failure (used when 404 is expected
                because the email was already moved in a prior pipeline run).
    """
    url = f"{GRAPH_BASE}/users/{TARGET_USER}/messages/{message_id}/move"
    payload = {"destinationId": folder_id}
    r = _request_with_retry("POST", url, headers=headers(token), json=payload)
    if r.status_code in (200, 201):
        return True
    if not silent:
        print(f"    [WARN] Move failed ({r.status_code}): {r.text[:200]}")
    return False


def get_messages_in_folder(token: str, folder_id: str, top: int = 200) -> list:
    """List all messages in a specific folder."""
    url = (
        f"{GRAPH_BASE}/users/{TARGET_USER}/mailFolders/{folder_id}/messages"
        f"?$top={top}&$select=id,subject,receivedDateTime,isRead"
        f"&$orderby=receivedDateTime desc"
    )
    r = _request_with_retry("GET", url, headers=headers(token))
    r.raise_for_status()
    return r.json().get("value", [])


def get_inbox_messages(token: str, top: int = 100) -> list:
    """List all messages in inbox."""
    url = (
        f"{GRAPH_BASE}/users/{TARGET_USER}/mailFolders/inbox/messages"
        f"?$top={top}&$select=id,subject,receivedDateTime,isRead"
        f"&$orderby=receivedDateTime desc"
    )
    r = _request_with_retry("GET", url, headers=headers(token))
    r.raise_for_status()
    return r.json().get("value", [])


# ─────────────────────────────────────────────────────────────────────────────
# Public API used by pipeline (outlook_poller / run_outlook_to_pipeline)
# ─────────────────────────────────────────────────────────────────────────────
_folder_cache: dict = {}

def get_folder_ids(token: str) -> dict:
    """
    Returns {'processed': <id>, 'unprocessed': <id>, 'exception': <id>}.
    Creates folders if they don't exist. Caches in memory.
    """
    global _folder_cache
    if _folder_cache:
        return _folder_cache
    processed   = get_or_create_folder(token, PROCESSED_FOLDER_NAME)
    unprocessed = get_or_create_folder(token, UNPROCESSED_FOLDER_NAME)
    exception   = get_or_create_folder(token, EXCEPTION_FOLDER_NAME)
    _folder_cache = {
        "processed":   processed["id"],
        "unprocessed": unprocessed["id"],
        "exception":   exception["id"],
    }
    return _folder_cache


def mark_as_processed(token: str, message_id: str, silent: bool = False) -> bool:
    """Move email to 'Processed POs' folder."""
    folders = get_folder_ids(token)
    ok = move_email_to_folder(token, message_id, folders["processed"], silent=silent)
    if ok:
        print(f"  -> Moved to 'Processed POs': {message_id[:20]}...")
    return ok


def mark_as_unprocessed(token: str, message_id: str, silent: bool = False) -> bool:
    """Move email to 'Unprocessed POs' folder."""
    folders = get_folder_ids(token)
    ok = move_email_to_folder(token, message_id, folders["unprocessed"], silent=silent)
    if ok:
        print(f"  -> Moved to 'Unprocessed POs': {message_id[:20]}...")
    return ok


def mark_as_exception(token: str, message_id: str, silent: bool = False) -> bool:
    """Move email to 'Exception POs' folder (mapping/extraction failures)."""
    folders = get_folder_ids(token)
    ok = move_email_to_folder(token, message_id, folders["exception"], silent=silent)
    if ok:
        print(f"  -> Moved to 'Exception POs': {message_id[:20]}...")
    return ok



# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def cmd_create_folders(token: str):
    """Step 1: Create the three folders."""
    print(f"\n{'='*60}")
    print(f"  Setting up mailbox folders for: {TARGET_USER}")
    print(f"{'='*60}")
    folders = get_folder_ids(token)
    print(f"\n  Folder IDs saved:")
    print(f"  Processed   : {folders['processed']}")
    print(f"  Unprocessed : {folders['unprocessed']}")
    print(f"  Exception   : {folders['exception']}")
    print(f"\n  [DONE] Folders are ready!\n")
    return folders


def safe_print(s: str):
    """Prints a string safely, falling back to ASCII replacement if CP1252/Windows console encoding fails."""
    try:
        print(s)
    except UnicodeEncodeError:
        print(s.encode('ascii', errors='replace').decode('ascii'))


def cmd_move_inbox_to_unprocessed(token: str):
    """Move all current inbox PO emails to Unprocessed POs folder."""
    folders = get_folder_ids(token)
    unprocessed_id = folders["unprocessed"]

    print(f"\n  Fetching inbox messages for {TARGET_USER}...")
    msgs = get_inbox_messages(token, top=100)
    print(f"  Found {len(msgs)} message(s) in inbox.")

    moved = 0
    for msg in msgs:
        subj = msg.get("subject", "")
        safe_print(f"\n  Moving: {subj[:70]}")
        if move_email_to_folder(token, msg["id"], unprocessed_id):
            moved += 1

    print(f"\n  Moved {moved}/{len(msgs)} email(s) to 'Unprocessed POs'.")


def cmd_status(token: str):
    """Show folder message counts."""
    folders = get_folder_ids(token)

    print(f"\n{'='*60}")
    print(f"  Mailbox Status: {TARGET_USER}")
    print(f"{'='*60}")

    folder_labels = {
        "processed": "Processed POs",
        "unprocessed": "Unprocessed POs",
        "exception": "Exception POs",
    }
    for key, folder_id in folders.items():
        label = folder_labels.get(key, key.upper())
        msgs = get_messages_in_folder(token, folder_id, top=200)
        print(f"\n  [{label.upper()}]  ({len(msgs)} emails)")
        for m in msgs[:10]:   # show latest 10
            subj = m.get("subject", "(no subject)")[:60]
            date = m.get("receivedDateTime", "")[:10]
            read = "READ" if m.get("isRead") else "UNREAD"
            safe_print(f"    {date}  [{read}]  {subj}")
        if len(msgs) > 10:
            print(f"    ... and {len(msgs)-10} more")

    # Also show inbox count
    inbox = get_inbox_messages(token, top=50)
    print(f"\n  [INBOX]  ({len(inbox)} emails remaining)")
    for m in inbox[:5]:
        subj = m.get("subject", "(no subject)")[:60]
        date = m.get("receivedDateTime", "")[:10]
        safe_print(f"    {date}  {subj}")


def main():
    parser = argparse.ArgumentParser(description="Outlook Folder Manager for salesorders mailbox")
    parser.add_argument("--move-all", action="store_true",
                        help="Move all inbox emails to 'Unprocessed POs' folder")
    parser.add_argument("--status",   action="store_true",
                        help="Show message counts in each folder")
    args = parser.parse_args()

    print("Getting Graph API token...")
    token = get_token()
    print(f"Token acquired ({len(token)} chars)")

    if args.status:
        cmd_status(token)
    elif args.move_all:
        cmd_create_folders(token)
        cmd_move_inbox_to_unprocessed(token)
    else:
        cmd_create_folders(token)
        print("  Tip: Run with --move-all to move existing inbox emails to 'Unprocessed POs'")
        print("  Tip: Run with --status to see folder contents\n")


if __name__ == "__main__":
    main()
