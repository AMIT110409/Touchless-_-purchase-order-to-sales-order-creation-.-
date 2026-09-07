"""
download_pos_by_sender.py
=========================
Downloads PO attachments from salesorders@envalior.com and organizes them into
subfolders grouped by the sender / CSR who sent the email.

Features:
  - Fetches emails from Graph API (Inbox or specified mail folder).
  - Extracts sender name & email from each message.
  - Creates a dedicated folder per CSR / Sender (e.g., downloaded_pos_by_csr/Tao_Ji/).
  - Downloads all PO attachments (.pdf, .xlsx, .xls, .docx, .doc, .png, .jpg).
  - Generates a summary report of PO counts grouped by sender / CSR.

Usage:
  python download_pos_by_sender.py                      # Process top 100 emails
  python download_pos_by_sender.py --top 200            # Process top 200 emails
  python download_pos_by_sender.py --dry-run            # Preview grouping without downloading
  python download_pos_by_sender.py --out-dir my_pos     # Custom output folder
"""

import os
import sys
import io
import re
import json
import base64
import argparse
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Force UTF-8 encoding for console output on Windows
if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# Import access token helper from outlook_poller
try:
    from outlook_poller import get_access_token, GRAPH_API_ENDPOINT, ATTACHMENT_EXTS, _graph_request, _strip_html, _looks_like_po_body
except ImportError:
    print("  [ERROR] outlook_poller.py not found in working directory.")
    sys.exit(1)


def sanitize_folder_name(name: str) -> str:
    """Sanitize sender name or email for safe folder path creation."""
    if not name:
        return "Unknown_Sender"
    # Remove invalid path characters: < > : " / \ | ? *
    clean = re.sub(r'[<>:"/\\|?*\n\r\t]', "_", name)
    clean = clean.strip().strip(".")
    return clean[:60] or "Unknown_Sender"


def download_pos_grouped_by_sender(
    top: int = 100,
    out_dir: str = "downloaded_pos_by_csr",
    mail_folder: str = None,
    dry_run: bool = False
):
    print("=" * 75)
    print("  PO Attachment Extractor — Grouped by Sender / CSR")
    print("=" * 75)

    base_out = Path(out_dir)
    if not dry_run:
        base_out.mkdir(parents=True, exist_ok=True)

    token = get_access_token()
    if not token:
        print("  [ERROR] Failed to acquire MS Graph API access token.")
        return

    headers = {"Authorization": f"Bearer {token}"}
    target_user = os.getenv("TARGET_EMAIL_USER", "salesorders@envalior.com")

    # 1. Resolve target mail folders (scan ALL folders: Inbox, Processed POs, Unprocessed POs, Exception POs, etc.)
    base_endpoint = f"{GRAPH_API_ENDPOINT}/users/{target_user}" if os.getenv("MS_GRAPH_CLIENT_SECRET") else f"{GRAPH_API_ENDPOINT}/me"
    folder_segments = []

    if mail_folder:
        folder_list_url = f"{base_endpoint}/mailFolders?$top=100"
        fr = requests.get(folder_list_url, headers=headers)
        if fr.status_code == 200:
            found = False
            for f in fr.json().get("value", []):
                if f.get("displayName", "").lower() == mail_folder.lower():
                    folder_segments.append((f.get("displayName"), f"/mailFolders/{f['id']}"))
                    print(f"Target Mail Folder: '{f.get('displayName')}'")
                    found = True
                    break
            if not found:
                print(f"[WARN] Folder '{mail_folder}' not found — using Inbox.")
                folder_segments.append(("Inbox", "/mailFolders/inbox"))
    else:
        # Default: scan ALL folders in the mailbox (Inbox, Processed POs, Unprocessed POs, Exception POs, etc.)
        folder_segments.append(("Inbox", "/mailFolders/inbox"))
        folder_list_url = f"{base_endpoint}/mailFolders?$top=100"
        fr = requests.get(folder_list_url, headers=headers)
        if fr.status_code == 200:
            for f in fr.json().get("value", []):
                fname = f.get("displayName", "")
                if fname.lower() != "inbox" and fname.lower() not in ("drafts", "sent items", "deleted items", "outbox", "junk email"):
                    folder_segments.append((fname, f"/mailFolders/{f['id']}"))

        folder_names_str = ", ".join(f"'{name}'" for name, _ in folder_segments)
        print(f"Scanning ALL Mail Folders ({len(folder_segments)} folders): {folder_names_str}\n")

    # 2. Fetch messages from all targeted folders
    messages = []
    seen_ids = set()

    for folder_label, f_seg in folder_segments:
        query = (
            f"$top={top}&$orderby=receivedDateTime desc"
            f"&$select=id,subject,receivedDateTime,hasAttachments,from,sender,body,bodyPreview"
        )
        url = f"{base_endpoint}{f_seg}/messages?{query}"

        print(f"Fetching top {top} messages from {target_user} [{folder_label}]...")
        resp = _graph_request("GET", url, headers=headers)

        if resp and resp.status_code == 200:
            for m in resp.json().get("value", []):
                if m.get("id") not in seen_ids:
                    seen_ids.add(m.get("id"))
                    messages.append(m)

    print(f"  Found {len(messages)} total unique message(s) to inspect.\n")

    # 3. Group and extract per sender
    sender_summary = {}  # sender_clean_name -> { "email": str, "emails_count": int, "po_files": [] }

    for idx, msg in enumerate(messages, 1):
        msg_id = msg.get("id", "")
        subject = msg.get("subject", "No Subject")
        
        # Extract Sender details
        from_info = msg.get("from", {}).get("emailAddress", {}) or msg.get("sender", {}).get("emailAddress", {})
        sender_name = from_info.get("name", "").strip()
        sender_email = from_info.get("address", "").strip()

        # Fallback if name is empty
        display_name = sender_name or sender_email or "Unknown_Sender"
        clean_folder_name = sanitize_folder_name(display_name)

        if clean_folder_name not in sender_summary:
            sender_summary[clean_folder_name] = {
                "display_name": display_name,
                "email": sender_email,
                "emails_count": 0,
                "po_files": []
            }
        sender_summary[clean_folder_name]["emails_count"] += 1

        sender_dir = base_out / clean_folder_name
        if not dry_run:
            sender_dir.mkdir(parents=True, exist_ok=True)

        print(f"[{idx}/{len(messages)}] Sender: '{display_name}' ({sender_email})")
        print(f"    Subject: {subject[:70]}")

        # A. Check Attachments
        if msg.get("hasAttachments", False):
            att_url = f"{base_endpoint}/messages/{msg_id}/attachments"
            att_resp = _graph_request("GET", att_url, headers=headers)

            if att_resp and att_resp.status_code == 200:
                for att in att_resp.json().get("value", []):
                    att_name = att.get("name", "")
                    if not att_name.lower().endswith(ATTACHMENT_EXTS):
                        continue
                    if att.get("@odata.type") != "#microsoft.graph.fileAttachment":
                        continue

                    content_b64 = att.get("contentBytes")
                    if not content_b64:
                        continue

                    # Skip tiny signature images
                    size_bytes = att.get("size", 0)
                    is_inline = att.get("isInline", False)
                    if att_name.lower().endswith((".png", ".jpg", ".jpeg")):
                        if is_inline and size_bytes < 80000:
                            continue
                        elif not is_inline and size_bytes < 15000:
                            continue

                    out_file = sender_dir / att_name
                    if out_file.exists():
                        out_file = sender_dir / f"{out_file.stem}_{msg_id[:8]}{out_file.suffix}"

                    if dry_run:
                        print(f"      [DRY-RUN] Would save attachment -> {clean_folder_name}/{out_file.name}")
                    else:
                        file_bytes = base64.b64decode(content_b64)
                        out_file.write_bytes(file_bytes)
                        print(f"      -> Saved PO attachment: {clean_folder_name}/{out_file.name}")

                    sender_summary[clean_folder_name]["po_files"].append(out_file.name)

        # B. Check Email Body PO
        body_content = msg.get("body", {})
        body_text = body_content.get("content", "") or ""
        if body_content.get("contentType") != "text":
            body_text = _strip_html(body_text)

        if body_text and _looks_like_po_body(body_text):
            body_filename = f"body_{msg_id[:8]}.txt"
            body_file = sender_dir / body_filename
            if dry_run:
                print(f"      [DRY-RUN] Would save body PO -> {clean_folder_name}/{body_filename}")
            else:
                body_file.write_text(body_text, encoding="utf-8")
                print(f"      -> Saved body PO: {clean_folder_name}/{body_filename}")
            sender_summary[clean_folder_name]["po_files"].append(body_filename)

    # 4. Summary Report
    print("\n" + "=" * 75)
    print(f"  PO Extraction Summary {'(DRY-RUN)' if dry_run else ''}")
    print("=" * 75)
    print(f"Total Senders / CSRs Identified: {len(sender_summary)}\n")

    print(f"{'Sender / CSR Name':<35} | {'Email':<30} | {'Emails':<7} | {'PO Files':<8}")
    print("-" * 88)

    total_files = 0
    for folder_name, info in sorted(sender_summary.items(), key=lambda x: len(x[1]["po_files"]), reverse=True):
        p_count = len(info["po_files"])
        total_files += p_count
        print(f"{info['display_name'][:34]:<35} | {info['email'][:29]:<30} | {info['emails_count']:<7} | {p_count:<8}")

    print("-" * 88)
    print(f"Total PO Files Extracted: {total_files}")
    print("=" * 75)

    if not dry_run:
        print(f"Files saved in directory: {base_out.resolve()}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Download PO attachments from salesorders@envalior.com grouped by Sender / CSR"
    )
    parser.add_argument("--top", type=int, default=100, help="Number of recent emails to process (default: 100)")
    parser.add_argument("--out-dir", type=str, default="downloaded_pos_by_csr", help="Output directory path")
    parser.add_argument("--mail-folder", type=str, default=None, help="Target mail folder name")
    parser.add_argument("--dry-run", action="store_true", help="Preview sender grouping without saving files")
    args = parser.parse_args()

    download_pos_grouped_by_sender(
        top=args.top,
        out_dir=args.out_dir,
        mail_folder=args.mail_folder,
        dry_run=args.dry_run
    )
