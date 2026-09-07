import msal
import requests
import os
os.environ['DISABLE_MODEL_SOURCE_CHECK'] = 'True'

import sys
import time
import json
import re
from pathlib import Path
from html import unescape
from dotenv import load_dotenv
from azure.identity import DefaultAzureCredential
from azure_email_tracker import AzureEmailTracker

# Force UTF-8 output on Windows to prevent charmap crashes on
# Chinese / emoji / non-ASCII characters in email subjects.
if sys.platform == "win32":
    import codecs
    try:
        sys.stdout = codecs.getwriter("utf-8")(sys.stdout.detach())
        sys.stderr = codecs.getwriter("utf-8")(sys.stderr.detach())
    except Exception:
        pass  # already detached or in a context where this isn't needed

# Load environment variables from .env file
load_dotenv()
from azure.storage.blob import BlobServiceClient


def _graph_request(method: str, url: str, headers: dict, max_retries: int = 3, **kwargs):
    """
    Wrapper around requests.get/patch that retries on connection errors
    with exponential backoff. Prevents RemoteDisconnected crashes on large mailboxes.
    """
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)
            return resp
        except (requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError,
                requests.exceptions.Timeout) as e:
            if attempt == max_retries:
                raise
            wait = 2 ** attempt  # 2s, 4s, 8s
            print(f"  [WARN] Graph API connection error (attempt {attempt}/{max_retries}): {e}. Retrying in {wait}s...")
            time.sleep(wait)

# ------------------------------------------------
# CONFIGURATION
# ------------------------------------------------
# Application (client) ID of your Azure App Registration
CLIENT_ID = os.getenv("MS_GRAPH_CLIENT_ID")
# Directory (tenant) ID
TENANT_ID = os.getenv("MS_GRAPH_TENANT_ID")
# Client Secret (if using Confidential Client / Service Principal) - Optional for Device Code
CLIENT_SECRET = os.getenv("MS_GRAPH_CLIENT_SECRET")

# Azure Blob Storage
BLOB_URL = os.getenv("AZURE_BLOB_URL")
INPUT_CONTAINER = "input-po"

# Supported attachment extensions (PO-related)
ATTACHMENT_EXTS = (".pdf", ".jpg", ".jpeg", ".png", ".xlsx", ".xls", ".docx", ".doc", ".msg", ".eml")

# Keywords that suggest email body contains PO content (e.g. Legrand format, Chinese orders, email-body POs)
PO_BODY_KEYWORDS = (
    "purchase order", "po#", "p.o.", "p.o #", "part#", "part number", "pn ",
    "quantity", "qty", "supplier", "bill to", "ship to", "release#", "release ",
    "due date", "price", "um", "description", "pass&seymour", "legrand",
    "order", "new order", "material", "sold-to", "ship-to", "delivery date", "volume",
    "订单", "新订单", "新增订单", "订货", "海星", "恩驊力", "件", "吨", "kg", "pcs",
    "bestellung", "liefertermin", "position", "menge",
)

# Subject prefixes that identify system-generated automated emails — NEVER re-process these
SKIP_SUBJECT_PREFIXES = (
    "[robona-attach]",        # Robona notification emails sent by the pipeline
    "[po exception",          # Exception routing emails sent by the pipeline
    "[po processed]",         # Processed confirmation emails
    "re: [robona",            # Replies to robona notifications
    "fwd: [robona",           # Forwards of robona notifications
    "re: [po exception",      # Replies to exception routing emails
    "fw: [po exception",      # Forwards of exception routing emails
    "undeliverable:",         # NDR bounce emails (e.g. CAMMI.ARTMAN.EXT not found)
    "undeliverable",          # NDR variations without colon
    "auto:",                  # Auto-reply emails
    "out of office:",         # Out of office replies
    "automatic reply:",       # Automatic replies
)

# Subject substrings that also identify system-generated / noise emails
# (for cases where the prefix check alone is not enough)
SKIP_SUBJECT_SUBSTRINGS = (
    "ai processing failed",
    "po exception - unknown",
    "[po exception -",
)

# DB statuses that mean the email has already been fully handled — skip even in --all mode
FINAL_STATUSES = (
    "ARCHIVED", "EXCEPTION_ROUTED", "MAPPED_SUCCESS", "SKIP",
    "FOLDER_MOVED", "PUSHED_TO_CELONIS", "PENDING_STAGE2", "ROBONA_SENT",
    "SO_BLOCKED", "SO_CREATION_FAILED", "MAPPING_FAILED", "EXTRACTION_FAILED"
)


# Outlook Scopes
SCOPES = ["https://graph.microsoft.com/.default"]  # For Client Credentials
GRAPH_API_ENDPOINT = "https://graph.microsoft.com/v1.0"


def _strip_html(html: str) -> str:
    """Remove HTML tags and decode entities to get plain text."""
    if not html:
        return ""
    text = re.sub(r"<[^>]+>", " ", html)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _looks_like_po_body(text: str) -> bool:
    """Heuristic: body contains PO-like content (e.g. Legrand Purchase Order Confirmation)."""
    if not text or len(text) < 100:
        return False
    lower = text.lower()
    return sum(1 for k in PO_BODY_KEYWORDS if k in lower) >= 2


def _safe_filename(s: str, max_len: int = 80) -> str:
    """Make string safe for use in filenames."""
    s = re.sub(r'[<>:"/\\|?*]', "_", s)
    s = s.strip()[:max_len] or "unnamed"
    return s


def get_access_token():
    """
    Acquire token via MSAL.
    Supports Client Credentials (Service Principal) or Device Code (User Interactive).
    """
    authority = f"https://login.microsoftonline.com/{TENANT_ID}"
    
    if CLIENT_SECRET:
        # Service Principal (Daemon App)
        app = msal.ConfidentialClientApplication(
            CLIENT_ID, authority=authority, client_credential=CLIENT_SECRET
        )
        result = app.acquire_token_for_client(scopes=SCOPES)
    else:
        # Device Code Flow (Run locally as User)
        # Note: App Registration must allow "Public Client" flows
        app = msal.PublicClientApplication(CLIENT_ID, authority=authority)
        accounts = app.get_accounts()
        if accounts:
            result = app.acquire_token_silent(["Mail.ReadWrite"], account=accounts[0])
        else:
            flow = app.initiate_device_flow(scopes=["Mail.ReadWrite"])
            if "user_code" not in flow:
                raise ValueError("Fail to create device flow. Err: %s" % json.dumps(flow, indent=4))
            print(flow["message"])
            result = app.acquire_token_by_device_flow(flow)
            
    if "access_token" in result:
        return result["access_token"]
    else:
        print(f"Error acquiring token: {result.get('error')}")
        print(f"Description: {result.get('error_description')}")
        return None

def upload_to_blob(filename: str, content: bytes) -> bool:
    """Upload file content (bytes) to Azure Blob Storage."""
    if not BLOB_URL:
        return False
    try:
        credential = DefaultAzureCredential()
        blob_service_client = BlobServiceClient(account_url=BLOB_URL, credential=credential)
        blob_client = blob_service_client.get_blob_client(container=INPUT_CONTAINER, blob=filename)
        blob_client.upload_blob(content, overwrite=True)
        print(f"  -> Uploaded {filename} to Blob Storage.")
        return True
    except Exception as e:
        print(f"  -> Blob Upload Error: {e}")
        return False


def process_emails(
    all_emails: bool = False,
    output_folder=None,
    upload_to_azure: bool = True,
    mark_read: bool = True,
    top: int = 100,
    mail_folder: str = None,          # e.g. "Unprocessed POs"
) -> list[str]:
    """
    Fetch emails, extract PO content from body and attachments, save to folder and/or Azure.

    Args:
        all_emails: If True, fetch all messages (not just unread). If False, only unread.
        output_folder: Local folder to save extracted files. Created if missing.
        upload_to_azure: If True and AZURE_BLOB_URL set, upload files to blob storage.
        mark_read: If True, mark processed emails as read (only when not all_emails).
        top: Max number of messages to process per run.

    Returns:
        List of saved file paths (relative to output_folder).
    """
    import base64

    # Initialize cloud-native tracker (Azure Table Storage, with SQLite fallback for local dev)
    tracker = AzureEmailTracker()

    token = get_access_token()
    if not token:
        return []

    headers = {"Authorization": f"Bearer {token}"}
    target_user = os.getenv("TARGET_EMAIL_USER")

    # Resolve folder ID if a named folder was given
    folder_segment = "/mailFolders/inbox"
    if mail_folder:
        import requests as _req
        folder_list_url = f"{GRAPH_API_ENDPOINT}/users/{target_user}/mailFolders?$top=100" if (CLIENT_SECRET and target_user) else f"{GRAPH_API_ENDPOINT}/me/mailFolders?$top=100"
        fr = _req.get(folder_list_url, headers={"Authorization": f"Bearer {token}"})
        if fr.status_code == 200:
            found_folder = False
            for f in fr.json().get("value", []):
                if f.get("displayName", "").lower() == mail_folder.lower():
                    folder_segment = f"/mailFolders/{f['id']}"
                    print(f"Targeting folder: '{mail_folder}' (id={f['id'][:20]}...)")
                    found_folder = True
                    break
            if not found_folder:
                print(f"[WARN] Folder '{mail_folder}' not found – falling back to inbox.")
        else:
            print(f"[WARN] Could not list mail folders ({fr.status_code}) – using inbox.")

    # Build query: always fetch both read and unread emails (no isRead eq false filter)
    # We rely on processed_emails.db to avoid reprocessing already finalised emails.
    query = f"$top={top}&$orderby=receivedDateTime desc&$select=id,subject,from,sender,receivedDateTime,hasAttachments,body,bodyPreview,conversationId"

    if CLIENT_SECRET and target_user:
        endpoint = f"{GRAPH_API_ENDPOINT}/users/{target_user}{folder_segment}/messages?{query}"
        base_msg = f"{GRAPH_API_ENDPOINT}/users/{target_user}/messages"
    else:
        endpoint = f"{GRAPH_API_ENDPOINT}/me{folder_segment}/messages?{query}"
        base_msg = f"{GRAPH_API_ENDPOINT}/me/messages"

    print(f"Fetching Outlook messages (both read and unread, top={top})...")
    response = _graph_request("GET", endpoint, headers=headers)
    if response.status_code != 200:
        print(f"Graph API Error: {response.status_code} - {response.text}")
        return []

    messages = response.json().get("value", [])
    print(f"  [Outlook] Found {len(messages)} message(s) in target folder.")

    # Keep track of message IDs that actually came from the polled folder (Inbox / target folder).
    # Only these messages need to be staged to the 'Unprocessed POs' folder.
    inbox_message_ids = {m["id"] for m in messages}

    # ── Also check the 'Unprocessed POs' folder ──────────────────────────────
    # If we are polling the Inbox (mail_folder is None), we also check the 'Unprocessed POs'
    # folder to make sure we don't miss any messages there.
    if not mail_folder:
        print(f"  [Outlook] Checking 'Unprocessed POs' folder for any pending/routed POs...")
        folder_list_url = f"{GRAPH_API_ENDPOINT}/users/{target_user}/mailFolders?$top=100" if (CLIENT_SECRET and target_user) else f"{GRAPH_API_ENDPOINT}/me/mailFolders?$top=100"
        fr = _graph_request("GET", folder_list_url, headers=headers)
        if fr.status_code == 200:
            unproc_id = None
            for fld in fr.json().get("value", []):
                if fld.get("displayName", "").lower() in ("unprocessed pos", "unprocessed po", "unprocessed purchase orders"):
                    unproc_id = fld["id"]
                    break
            if unproc_id:
                # Fetch messages from 'Unprocessed POs' (they may already be read)
                unproc_url = f"{GRAPH_API_ENDPOINT}/users/{target_user}/mailFolders/{unproc_id}/messages?{query}" if (CLIENT_SECRET and target_user) else f"{GRAPH_API_ENDPOINT}/me/mailFolders/{unproc_id}/messages?{query}"
                ur = _graph_request("GET", unproc_url, headers=headers)
                if ur.status_code == 200:
                    unproc_messages = ur.json().get("value", [])
                    print(f"  [Outlook] Found {len(unproc_messages)} message(s) in 'Unprocessed POs' folder.")
                    
                    # Merge and deduplicate by conversationId / message metadata
                    def _msg_dedup_key(m: dict) -> str:
                        cid = m.get("conversationId", "") or ""
                        if cid:
                            return f"cid:{cid}"
                        sbj = (m.get("subject", "") or "").strip().lower()
                        snd = ((m.get("from", {}) or {}).get("emailAddress", {}) or {}).get("address", "").strip().lower()
                        rdt = (m.get("receivedDateTime", "") or "")[:19]
                        return f"meta:{sbj}|{snd}|{rdt}"

                    seen_msg_keys = {_msg_dedup_key(m) for m in messages}
                    added_count = 0
                    for um in unproc_messages:
                        ukey = _msg_dedup_key(um)
                        if ukey not in seen_msg_keys and um["id"] not in inbox_message_ids:
                            messages.append(um)
                            seen_msg_keys.add(ukey)
                            added_count += 1
                    if added_count > 0:
                        print(f"  [Outlook] Added {added_count} unique message(s) from 'Unprocessed POs' folder.")
                else:
                    print(f"  [Outlook] Could not read 'Unprocessed POs': {ur.status_code}")
            else:
                print(f"  [Outlook] 'Unprocessed POs' folder not found in mailbox.")
        else:
            print(f"  [Outlook] Could not list mail folders ({fr.status_code})")

    # Final pass: deduplicate the messages list by conversation/metadata key
    def _final_msg_key(m: dict) -> str:
        cid = m.get("conversationId", "") or ""
        if cid:
            return f"cid:{cid}"
        sbj = (m.get("subject", "") or "").strip().lower()
        snd = ((m.get("from", {}) or {}).get("emailAddress", {}) or {}).get("address", "").strip().lower()
        rdt = (m.get("receivedDateTime", "") or "")[:19]
        return f"meta:{sbj}|{snd}|{rdt}"

    unique_messages = []
    seen_final_keys = set()
    for m in messages:
        k = _final_msg_key(m)
        if k not in seen_final_keys:
            seen_final_keys.add(k)
            unique_messages.append(m)
    messages = unique_messages

    if not messages:
        print(f"  [Outlook] Found 0 message(s) in mailbox: {target_user}")
    else:
        print(f"Total unique messages to process: {len(messages)}")


    # ── Stage new inbox emails into 'Unprocessed POs' folder ─────────────────
    # This makes 'Unprocessed POs' the canonical entry queue.
    # Emails are moved BEFORE attachments are extracted so that:
    #   - The pipeline metrics report "X emails from Unprocessed POs"
    #   - Any email left in 'Unprocessed POs' at end of run = retry candidate
    # Only stage emails coming from the Inbox (not if already reading from a named folder).
    staged_to_unprocessed = 0
    if messages and not mail_folder:
        # Look up 'Unprocessed POs' folder ID (already fetched above if it exists)
        _unproc_folder_id = None
        _fl_url = (
            f"{GRAPH_API_ENDPOINT}/users/{target_user}/mailFolders?$top=100"
            if (CLIENT_SECRET and target_user)
            else f"{GRAPH_API_ENDPOINT}/me/mailFolders?$top=100"
        )
        _fl_r = _graph_request("GET", _fl_url, headers=headers)
        if _fl_r.status_code == 200:
            for _fld in _fl_r.json().get("value", []):
                if _fld.get("displayName", "").lower() in (
                    "unprocessed pos", "unprocessed po", "unprocessed purchase orders"
                ):
                    _unproc_folder_id = _fld["id"]
                    break

        if _unproc_folder_id:
            _move_base = (
                f"{GRAPH_API_ENDPOINT}/users/{target_user}/messages"
                if (CLIENT_SECRET and target_user)
                else f"{GRAPH_API_ENDPOINT}/me/messages"
            )
            for _msg in messages:
                _mid = _msg["id"]
                
                # ONLY stage messages that actually came from Inbox/target folder (not already in Unprocessed POs)
                if _mid not in inbox_message_ids:
                    continue

                _move_url = f"{_move_base}/{_mid}/move"
                _mv = _graph_request(
                    "POST", _move_url, headers=headers,
                    json={"destinationId": _unproc_folder_id}
                )
                if _mv.status_code in (200, 201):
                    staged_to_unprocessed += 1
                    try:
                        res_json = _mv.json()
                        if "id" in res_json:
                            _msg["id"] = res_json["id"]
                            # Since the message got a new ID, update the inbox_message_ids set with the new ID
                            # so that subsequent code matching uses the active ID.
                            inbox_message_ids.remove(_mid)
                            inbox_message_ids.add(res_json["id"])
                    except Exception as _e:
                        print(f"  [WARN] Could not parse moved email response: {_e}")
                # If 404: email already moved or deleted — skip silently
            if staged_to_unprocessed:
                print(f"  [Outlook] Staged {staged_to_unprocessed} email(s) to 'Unprocessed POs' folder.")
        else:
            print("  [Outlook] 'Unprocessed POs' folder not found — skipping staging step.")


    out_dir = Path(output_folder) if output_folder else Path("outlook_po_extracted")
    out_dir.mkdir(parents=True, exist_ok=True)
    saved_files = []


    for msg in messages:
        msg_id = msg["id"]

        subject = msg.get("subject", "No Subject")
        received_date = msg.get("receivedDateTime", "")

        # -- Skip system-generated automated emails (ROBONA-ATTACH / PO Exception / NDR bounces) --
        subject_lower = subject.lower().strip()
        if any(subject_lower.startswith(prefix) for prefix in SKIP_SUBJECT_PREFIXES):
            print(f"\nSkipping automated email (prefix match): {subject[:80]}")
            continue
        if any(sub in subject_lower for sub in SKIP_SUBJECT_SUBSTRINGS):
            print(f"\nSkipping automated email (substring match): {subject[:80]}")
            continue

        # -- Skip already-finalised emails (even in --all mode) --
        existing = tracker.get_email(msg_id)
        if existing:
            row_status = existing.get("status", "")
            if row_status in FINAL_STATUSES:
                print(f"\nSkipping {msg_id[:8]}... (already finalised: {row_status})")
                continue

        print(f"\nProcessing: {subject}")

        # Upsert into tracker — stores conversationId & sender_email for Stage 2 & Celonis push.
        # upsert_email uses INSERT-or-MERGE so existing records are not overwritten.
        conversation_id = msg.get("conversationId", "") or ""
        sender_obj = msg.get("from", {}).get("emailAddress", {}) or msg.get("sender", {}).get("emailAddress", {})
        sender_email = sender_obj.get("address", "") or ""

        # Fallback: extract original sender email address from body if Graph API sender is generic/empty
        if not sender_email or "salesorders@" in sender_email.lower():
            body_obj = msg.get("body", {}) or {}
            body_text_raw = body_obj.get("content", "") or ""
            # Match "From: ... <email@domain>" or "From: email@domain"
            _m = re.search(r'(?i)\bFrom:\s*.*?\b([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})\b', body_text_raw)
            if _m and "salesorders@" not in _m.group(1).lower():
                sender_email = _m.group(1)

        tracker.upsert_email(
            msg_id,
            subject=subject,
            received_date=received_date,
            status="PENDING",
            conversation_id=conversation_id,
            sender_email=sender_email,
        )


        # Helper: record a saved filename into the tracker (appended to 'attachments' property)
        def _record_attachment(filename: str):
            try:
                tracker.record_attachment(msg_id, filename)
            except Exception:
                pass

        # -- 1. Email body: save if it looks like PO (e.g. Legrand body format) --
        body_content = msg.get("body", {})
        body_text = ""
        if body_content.get("contentType") == "text":
            body_text = body_content.get("content", "") or ""
        else:
            body_text = _strip_html(body_content.get("content", "") or "")
        if body_text and _looks_like_po_body(body_text):
            safe_subj = _safe_filename(subject)
            body_filename = f"body_{msg_id[:8]}_{safe_subj}.txt"
            body_path = out_dir / body_filename
            body_path.write_text(body_text, encoding="utf-8")
            saved_files.append(str(body_path))
            print(f"  -> Saved body as PO: {body_filename}")
            _record_attachment(body_filename)  # <-- Track in DB for post-processing
            if upload_to_azure:
                upload_to_blob(body_filename, body_path.read_bytes())

        # -- 2. Attachments: PDF, Excel, Word, images, .msg, .eml, itemAttachment --
        att_endpoint = f"{base_msg}/{msg_id}/attachments"
        att_resp = _graph_request("GET", att_endpoint, headers=headers)
        if att_resp.status_code == 200:
            for att in att_resp.json().get("value", []):
                att_name = att.get("name", "")
                att_type = att.get("@odata.type", "")

                # Handle itemAttachment (attached emails in .msg format)
                if att_type == "#microsoft.graph.itemAttachment":
                    att_id = att.get("id", "")
                    if not att_name:
                        att_name = f"attached_email_{att_id[:8]}.msg"
                    elif not att_name.lower().endswith((".msg", ".eml")):
                        att_name = f"{att_name}.msg"

                    # Clean filename — remove Windows-illegal chars
                    att_name = re.sub(r'[<>:"/\\|?*\[\]]', "_", att_name).strip()
                    if not att_name:
                        att_name = f"attached_email_{att_id[:8]}.msg"

                    # Skip saving system-generated exception emails embedded as attachments
                    # Note: after re.sub above, '[po exception' becomes '_po exception'
                    _aname_lower = att_name.lower()
                    _SKIP_ATT_SUBS = (
                        'po exception',       # catches both '[po exception' and '_po exception'
                        'robona-attach',
                        'ai processing failed',
                        'undeliverable_ _po',  # sanitized NDR subject
                    )
                    if any(s in _aname_lower for s in _SKIP_ATT_SUBS):
                        print(f"  [Skip] Embedded exception email attachment — not a PO: {att_name[:70]}")
                        continue

                    val_url = f"{att_endpoint}/{att_id}/$value"
                    val_resp = _graph_request("GET", val_url, headers=headers)
                    att_bytes = val_resp.content if (val_resp.status_code == 200 and val_resp.content) else None

                    if not att_bytes:
                        content_b64 = att.get("contentBytes")
                        att_bytes = base64.b64decode(content_b64) if content_b64 else None

                    if att_bytes:
                        att_path = out_dir / att_name
                        if att_path.exists():
                            stem, suf = att_path.stem, att_path.suffix
                            att_path = out_dir / f"{stem}_{msg_id[:8]}{suf}"
                        att_path.write_bytes(att_bytes)
                        saved_files.append(str(att_path))
                        print(f"  -> Saved attached email: {att_path.name}")
                        _record_attachment(att_path.name)
                        if upload_to_azure:
                            upload_to_blob(att_path.name, att_bytes)
                    continue

                if att_type != "#microsoft.graph.fileAttachment":
                    continue

                if not att_name.lower().endswith(ATTACHMENT_EXTS):
                    print(f"  Skipping {att_name} (unsupported type)")
                    continue
                content_b64 = att.get("contentBytes")
                if not content_b64:
                    continue
                
                # Check for Inline signatures / tiny logos
                is_inline = att.get("isInline", False)
                size_bytes = att.get("size", 0)
                
                if att_name.lower().endswith((".png", ".jpg", ".jpeg")):
                    # Skip ONLY small inline images (logos / email signatures < 15KB).
                    # Real PO scans / image POs embedded in email body are >= 15KB.
                    if is_inline and size_bytes < 15000:
                        print(f"  Skipping {att_name} (Signature/logo: {size_bytes}b, inline={is_inline})")
                        continue
                    elif not is_inline and size_bytes < 5000:
                        # Tiny non-inline images (< 5KB) are decorative icons
                        print(f"  Skipping {att_name} (Tiny icon: {size_bytes}b)")
                        continue

                content_bytes = base64.b64decode(content_b64)
                att_path = out_dir / att_name

                # Deduplicate: if target file or matching file already exists, check content size
                if att_path.exists():
                    if att_path.stat().st_size == len(content_bytes):
                        print(f"  [DUP-SKIP] Attachment '{att_name}' already exists with identical size ({len(content_bytes):,}b) — skipping duplicate save.")
                        if str(att_path) not in saved_files:
                            saved_files.append(str(att_path))
                        _record_attachment(att_path.name)
                        continue
                    else:
                        # File with same name but DIFFERENT size exists — generate suffix
                        stem, suf = att_path.stem, att_path.suffix
                        att_path = out_dir / f"{stem}_{msg_id[:8]}{suf}"

                att_path.write_bytes(content_bytes)
                saved_files.append(str(att_path))
                print(f"  -> Saved attachment: {att_path.name}")
                _record_attachment(att_path.name)  # <-- Track in DB for post-processing
                if upload_to_azure:
                    upload_to_blob(att_path.name, content_bytes)


        if mark_read and not all_emails:
            patch_endpoint = f"{base_msg}/{msg_id}"
            try:
                _graph_request("PATCH", patch_endpoint, headers=headers, json={"isRead": True})
            except Exception as _e:
                print(f"  [WARN] Could not mark email as read: {_e}")
            print(f"  Marked as read.")

    print(f"\nExtracted {len(saved_files)} file(s) to {out_dir}")
    return saved_files


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Outlook PO Extractor - body + attachments")
    parser.add_argument("--all", action="store_true", help="Process all emails (not just unread)")
    parser.add_argument("--folder", type=str, default="outlook_po_extracted", help="Output folder")
    parser.add_argument("--no-azure", action="store_true", help="Skip Azure blob upload")
    parser.add_argument("--no-mark-read", action="store_true", help="Do not mark emails as read")
    parser.add_argument("--once", action="store_true", help="Run once and exit (no polling loop)")
    parser.add_argument("--top", type=int, default=100, help="Max messages per run")
    parser.add_argument("--mail-folder", type=str, default=None,
                        help="Read from a specific Outlook folder (e.g. 'Unprocessed POs')")
    args = parser.parse_args()

    if not os.getenv("AZURE_BLOB_URL") and not args.no_azure:
        print("Warning: AZURE_BLOB_URL not set. Use --no-azure to run without upload.")

    def run():
        process_emails(
            all_emails=args.all,
            output_folder=args.folder,
            upload_to_azure=bool(os.getenv("AZURE_BLOB_URL")) and not args.no_azure,
            mark_read=not args.no_mark_read,
            top=args.top,
            mail_folder=args.mail_folder,
        )

    if args.once:
        run()
    else:
        while True:
            try:
                run()
            except Exception as e:
                print(f"Loop Error: {e}")
            print("Sleeping 60s...")
            time.sleep(60)
