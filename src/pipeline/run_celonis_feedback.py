"""
=============================================================================
Stage 2 Celonis Feedback — SO Creation Results Handler
=============================================================================
This script is the SECOND STAGE of the two-stage exception handling system.

It runs AFTER the Celonis Action Flow has had time to process the PO extraction
results and attempt SAP Sales Order creation (typically 30–60 minutes after
the main pipeline run).

Flow:
  1. Query Celonis for SO creation results (success / blocked / failed)
  2. For each PO result, match back to the original email in processed_emails.db
  3. Route notifications:
     - SO created, no block     → Send Robona notification → DB: ROBONA_SENT
     - SO created, order block  → Send Stage 2 CSR email   → DB: SO_BLOCKED
     - SO creation failed       → Send Stage 2 CSR email + move to Exception POs → DB: SO_CREATION_FAILED

Usage:
  python run_celonis_feedback.py
  python run_celonis_feedback.py --dry-run            # log actions only, no API calls
  python run_celonis_feedback.py --po-number PO-1234  # process a single PO for testing

Configuration:
  Set SO_RESULTS_TABLE in this file (or via .env SO_RESULTS_TABLE) to match
  the Celonis view/table name where Action Flow writes SO creation outcomes.

  Expected columns in the Celonis SO results table:
    PO_NUMBER        : matches the po_number extracted by the pipeline
    SO_NUMBER        : SAP Sales Order number (empty if creation failed)
    STATUS           : 'SUCCESS', 'BLOCKED', 'FAILED'
    BLOCK_CODE       : SAP block code (e.g. '01' for credit block) — nullable
    BLOCK_REASON     : human-readable reason for the block — nullable
    FAILURE_REASON   : reason if STATUS='FAILED' — nullable
    SALES_ORG        : SAP Sales Org code for regional routing
    RUN_TIMESTAMP    : when the Action Flow ran
=============================================================================
"""

import os
import sys
import io
import json
import hashlib
import argparse
import requests
from pathlib import Path
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Force UTF-8 encoding for stdout and stderr to prevent Windows console UnicodeEncodeErrors
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

load_dotenv()

# ───────────────────────────────────────────────────────────────────────────────
# Configuration  (all values from .env — no hardcoded credentials)
# ───────────────────────────────────────────────────────────────────────────────
CELONIS_URL        = os.getenv("CELONIS_URL")
CELONIS_API_TOKEN  = os.getenv("CELONIS_API_TOKEN")
CELONIS_POOL_ID    = os.getenv("CELONIS_POOL_ID")

# ── CONFIGURE THIS: name of the Celonis table/view with Action Flow SO results ──
SO_RESULTS_TABLE   = os.getenv("SO_RESULTS_TABLE",   "SO_CREATION_RESULTS")

REGIONAL_CONFIG    = Path(__file__).parent / "regional_config.json"

# Statuses that are eligible for Stage 2 processing
STAGE2_ELIGIBLE    = ("PUSHED_TO_CELONIS",)

# ─────────────────────────────────────────────────────────────────────────────
# Regional routing
# ─────────────────────────────────────────────────────────────────────────────
def load_regional_config() -> dict:
    if REGIONAL_CONFIG.exists():
        with open(REGIONAL_CONFIG) as f:
            return json.load(f)
    return {}


def resolve_region(sales_org: str, regional_config: dict) -> dict:
    if sales_org:
        for region, config in regional_config.items():
            if sales_org in config.get("sales_orgs", []):
                return {"region": region, **config}
    fallback = regional_config.get("DEFAULT", {
        "cs_email": os.getenv("DEFAULT_CS_EMAIL", "cs-test@envalior.com"),
        "region": "DEFAULT"
    })
    return {"region": "DEFAULT", **fallback}


# ─────────────────────────────────────────────────────────────────────────────
# Fetch SO results — from Azure Blob (primary) or directly from Celonis (fallback)
# ─────────────────────────────────────────────────────────────────────────────
def fetch_so_results_from_azure(hours_back: int = 168, no_time_filter: bool = False) -> list[dict]:
    """
    Primary path: Read SO creation results from Azure Blob Storage.
    The blob 'so_creation_results.parquet' is populated by celonis_to_azure.py
    (run after the Celonis Action Flow completes).

    Falls back to direct Celonis query if blob not found.

    Args:
        hours_back:      Only include rows whose RUN_TIMESTAMP is within the last N hours.
                         Default is 168h (7 days) to cover weekend/Friday backlog runs.
        no_time_filter:  If True, load ALL rows regardless of timestamp (manual catch-up mode).
    """
    print(f"\n[Stage 2] Fetching SO results from Azure Blob Storage...")
    if no_time_filter:
        print(f"  [INFO] --no-time-filter active: loading ALL rows from blob (no time cutoff).")
    try:
        from azure_table_reader import AzureTableReader
        reader = AzureTableReader()
        df = reader.get_so_results()

        if df is None or df.empty:
            print("  [INFO] SO results blob is empty — no Action Flow results yet.")
            return []

        total_rows = len(df)

        # Filter to last N hours if RUN_TIMESTAMP exists (skip if --no-time-filter)
        if not no_time_filter and "RUN_TIMESTAMP" in df.columns:
            from datetime import datetime, timedelta
            cutoff = datetime.now() - timedelta(hours=hours_back)
            df["RUN_TIMESTAMP"] = df["RUN_TIMESTAMP"].astype(str)
            recent_mask = df["RUN_TIMESTAMP"] >= cutoff.strftime("%Y-%m-%d %H:%M:%S")
            df_filtered = df[recent_mask]
            if df_filtered.empty:
                # Time-window returned nothing — fall back to ALL rows so we never miss old SOs
                print(f"  [WARN] No SO results in the last {hours_back}h "
                      f"(last run: {df['RUN_TIMESTAMP'].max()}). "
                      f"Loading ALL {total_rows} row(s) from blob as fallback.")
                # df stays as-is (all rows)
            else:
                df = df_filtered
                print(f"  [INFO] Filtered to last {hours_back}h: {len(df)} row(s) "
                      f"(of {total_rows} total in blob)")
        else:
            if "RUN_TIMESTAMP" in df.columns:
                df["RUN_TIMESTAMP"] = df["RUN_TIMESTAMP"].astype(str)
            print(f"  [INFO] Loaded all {total_rows} row(s) from blob (no time filter applied).")

        # Save local copy as CSV for easy user inspection
        try:
            csv_local = Path("so_creation_results_local.csv")
            df.to_csv(csv_local, index=False, encoding="utf-8-sig")
            print(f"  [OK] Saved local CSV copy -> {csv_local.name}")
        except Exception:
            pass

        rows = df.to_dict(orient="records")
        print(f"  [OK] Loaded {len(rows)} SO result(s) from Azure Blob.")
        return rows

    except FileNotFoundError:
        print("  [WARN] Azure Blob 'so_creation_results.parquet' not found.")
        print("  [INFO] Run: python celonis_to_azure.py --so-results-only")
        print("  [INFO] Falling back to direct Celonis query...")
        return _fetch_so_results_direct_from_celonis(hours_back, no_time_filter)
    except Exception as e:
        print(f"  [WARN] Azure Blob read failed: {e}")
        print("  [INFO] Falling back to direct Celonis query...")
        return _fetch_so_results_direct_from_celonis(hours_back, no_time_filter)


def _fetch_so_results_direct_from_celonis(hours_back: int = 168, no_time_filter: bool = False) -> list[dict]:
    """
    Fallback path: Query Celonis directly for SO results.
    Used when the Azure Blob is not yet populated.
    """
    print(f"  [Celonis] Querying '{SO_RESULTS_TABLE}' directly from Celonis Data Pool...")
    try:
        from pycelonis import get_celonis
        c = get_celonis(CELONIS_URL, CELONIS_API_TOKEN)
        data_pool = c.data_integration.get_data_pool(CELONIS_POOL_ID)

        existing = [t.name for t in data_pool.get_tables()]
        if SO_RESULTS_TABLE not in existing:
            print(f"  [WARN] Table '{SO_RESULTS_TABLE}' not found in Celonis pool.")
            print(f"  [INFO] Available tables: {existing[:15]}")
            print(f"  [INFO] Set SO_RESULTS_TABLE in .env to match your Action Flow output table.")
            return []

        table_obj = data_pool.get_table(SO_RESULTS_TABLE)
        df = table_obj.get_data_frame()

        if df is None or df.empty:
            print("  [INFO] SO results table is empty.")
            return []

        # Normalize column names
        df.columns = [c.upper() for c in df.columns]

        # Filter to recent rows (skip if --no-time-filter)
        if not no_time_filter and "RUN_TIMESTAMP" in df.columns:
            from datetime import datetime, timedelta
            cutoff = datetime.now() - timedelta(hours=hours_back)
            df["RUN_TIMESTAMP"] = df["RUN_TIMESTAMP"].astype(str)
            df_filtered = df[df["RUN_TIMESTAMP"] >= cutoff.strftime("%Y-%m-%d %H:%M:%S")]
            if df_filtered.empty:
                print(f"  [WARN] No Celonis rows in last {hours_back}h — loading all rows as fallback.")
            else:
                df = df_filtered

        rows = df.to_dict(orient="records")
        print(f"  [OK] Fetched {len(rows)} SO result(s) directly from Celonis.")
        return rows

    except ImportError:
        print("  [ERROR] pycelonis not installed. Run: pip install pycelonis")
        return []
    except Exception as e:
        print(f"  [ERROR] Direct Celonis query failed: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Graph API: Send emails via MS Graph
# ─────────────────────────────────────────────────────────────────────────────
def get_graph_token() -> str:
    try:
        import msal
        app = msal.ConfidentialClientApplication(
            os.getenv("MS_GRAPH_CLIENT_ID"),
            authority=f"https://login.microsoftonline.com/{os.getenv('MS_GRAPH_TENANT_ID')}",
            client_credential=os.getenv("MS_GRAPH_CLIENT_SECRET")
        )
        result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        if "access_token" in result:
            return result["access_token"]
    except Exception as e:
        print(f"  [ERROR] Graph token error: {e}")
    return ""


def download_po_pdf(source_file: str, dry_run: bool = False) -> bytes | None:
    """
    Download the PO PDF for attaching to the Robona notification email.

    Resolution order:
      1. Account Key connection string (AZURE_STORAGE_ACCOUNT_KEY + NAME from .env) — no az login
      2. AZURE_STORAGE_CONNECTION_STRING env var
      3. Azure SDK with DefaultAzureCredential (works in Azure cloud / managed identity)
      4. Local file — outlook_po_archive/ folder  (pipeline archives PDFs here after processing)
      5. Local file — outlook_po_attachments/ folder (active download folder)
      6. Local file — outlook_po_current_run/ folder (staging folder)
      7. Local file — current working directory
      8. Direct HTTP GET (only works if blob container is public)

    Returns raw PDF bytes, or None if all methods fail.
    """
    if dry_run:
        print(f"  [DRY-RUN] Skipping PO PDF download for '{source_file}'.")
        return None
    if not source_file or source_file in ("nan", "None", ""):
        print("  [PDF-WARN] source_file is empty — cannot attach PDF to Robona email.")
        return None

    azure_blob_url = os.getenv("AZURE_BLOB_URL", "https://poextstorage49245.blob.core.windows.net")
    container      = "input-po"
    script_dir     = os.path.dirname(os.path.abspath(__file__))

    # ── Method 1: Account Key (from .env) — no az login required ─────────
    _acct_name = os.getenv("AZURE_STORAGE_ACCOUNT_NAME", "").strip()
    _acct_key  = os.getenv("AZURE_STORAGE_ACCOUNT_KEY",  "").strip()
    if _acct_name and _acct_key:
        _built_conn = (
            f"DefaultEndpointsProtocol=https;"
            f"AccountName={_acct_name};"
            f"AccountKey={_acct_key};"
            f"EndpointSuffix=core.windows.net"
        )
        try:
            from azure.storage.blob import BlobServiceClient
            bsc  = BlobServiceClient.from_connection_string(_built_conn)
            data = bsc.get_blob_client(container=container, blob=source_file).download_blob().readall()
            print(f"  [PDF] Downloaded '{source_file}' ({len(data):,} bytes) via Account Key.")
            return data
        except Exception as e:
            print(f"  [PDF-WARN] Account Key download failed: {e}")

    # ── Method 2: Connection string env var ───────────────────────────────
    conn_str = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "")
    if conn_str:
        try:
            from azure.storage.blob import BlobServiceClient
            bsc  = BlobServiceClient.from_connection_string(conn_str)
            data = bsc.get_blob_client(container=container, blob=source_file).download_blob().readall()
            print(f"  [PDF] Downloaded '{source_file}' ({len(data):,} bytes) via connection string.")
            return data
        except Exception as e:
            print(f"  [PDF-WARN] Connection string download failed: {e}")

    # ── Method 3: Azure SDK with DefaultAzureCredential ──────────────────
    try:
        import logging
        for noisy_logger in ("azure.identity", "azure.identity._credentials",
                             "azure.core.pipeline.policies.http_logging_policy"):
            logging.getLogger(noisy_logger).setLevel(logging.CRITICAL)

        from azure.storage.blob import BlobServiceClient
        from azure.identity import DefaultAzureCredential
        bsc  = BlobServiceClient(account_url=azure_blob_url, credential=DefaultAzureCredential())
        data = bsc.get_blob_client(container=container, blob=source_file).download_blob().readall()
        print(f"  [PDF] Downloaded '{source_file}' ({len(data):,} bytes) from Azure Blob.")
        return data
    except Exception as e:
        print(f"  [PDF-WARN] Azure DefaultAzureCredential failed: {type(e).__name__}. Trying local fallback...")

    # ── Methods 4-7: Local file fallback ─────────────────────────────────
    # The pipeline saves PDFs locally before uploading to Azure Blob.
    # Check all local folders where PDFs may exist.
    local_search = [
        os.path.join(script_dir, "outlook_po_archive",      source_file),
        os.path.join(script_dir, "outlook_po_attachments",  source_file),
        os.path.join(script_dir, "outlook_po_current_run",  source_file),
        os.path.join(script_dir,                            source_file),
        # Case-insensitive: try lowercase filename too
        os.path.join(script_dir, "outlook_po_archive",      source_file.lower()),
        os.path.join(script_dir, "outlook_po_attachments",  source_file.lower()),
        os.path.join(script_dir, "outlook_po_current_run",  source_file.lower()),
    ]
    for local_path in local_search:
        if os.path.isfile(local_path):
            try:
                with open(local_path, "rb") as fh:
                    data = fh.read()
                print(f"  [PDF] Loaded from local file: {local_path} ({len(data):,} bytes)")
                return data
            except Exception as e:
                print(f"  [PDF-WARN] Could not read local file {local_path}: {e}")

    # ── Method 8: Direct HTTP (only works for public blobs) ───────────────
    blob_url = f"{azure_blob_url}/{container}/{source_file}"
    try:
        r = requests.get(blob_url, timeout=30)
        if r.status_code == 200:
            print(f"  [PDF] Downloaded via HTTP: {len(r.content):,} bytes")
            return r.content
        else:
            print(f"  [PDF-WARN] HTTP GET returned {r.status_code} for {blob_url}")
    except Exception as e:
        print(f"  [PDF-WARN] HTTP fallback failed: {e}")

    print(f"  [PDF-WARN] All download methods failed for '{source_file}' — trying direct Graph API attachment fallback.")
    return None


def fetch_graph_message_pdf_attachment(token: str, mailbox: str, message_id: str) -> tuple[bytes | None, str | None]:
    """Fetch raw PDF attachment bytes directly from Graph API message attachments."""
    if not token or not mailbox or not message_id or not message_id.startswith(("AAMk", "AQMk")):
        return None, None
    try:
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        url = f"https://graph.microsoft.com/v1.0/users/{mailbox}/messages/{message_id}/attachments"
        r = requests.get(url, headers=headers, timeout=30)
        if r.status_code == 200:
            atts = r.json().get("value", [])
            for att in atts:
                fname = att.get("name", "")
                if fname.lower().endswith(".pdf") and "contentBytes" in att:
                    import base64
                    pdf_data = base64.b64encode(att["contentBytes"])
                    print(f"  [PDF] Fetched '{fname}' ({len(pdf_data):,} bytes) directly via Graph API attachment!")
                    return pdf_data, fname
    except Exception as e:
        print(f"  [PDF-WARN] Graph API attachment fetch failed: {e}")
    return None, None


def forward_original_email_to_robona(token: str, mailbox: str, robona_mailbox: str, message_id: str, so_number: str, dry_run: bool = False) -> bool:
    """Forward original incoming PO email thread to Roborana."""
    if not message_id or not message_id.startswith(("AAMk", "AQMk")):
        return False
    if dry_run:
        print(f"    [DRY-RUN] Would forward original email ({message_id[:20]}...) to {robona_mailbox}")
        return True
    try:
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        url = f"https://graph.microsoft.com/v1.0/users/{mailbox}/messages/{message_id}/forward"
        payload = {
            "comment": f"[Touchless Order Agent] Original customer email forwarded for archiving in SAP against Sales Order {so_number}.",
            "toRecipients": [{"emailAddress": {"address": robona_mailbox}}]
        }
        r = requests.post(url, headers=headers, json=payload, timeout=30)
        if r.status_code == 202:
            print(f"  [OK] Forwarded original email ({message_id[:20]}...) to Roborana ({robona_mailbox}).")
            return True
        else:
            print(f"  [WARN] Email forward failed ({r.status_code}): {r.text[:150]}")
    except Exception as e:
        print(f"  [WARN] Email forward error: {e}")
    return False


def send_email_via_graph(token: str, mailbox: str, to_address: str | list,
                         subject: str, body_html: str, dry_run: bool = False,
                         pdf_bytes: bytes | None = None,
                         pdf_filename: str | None = None,
                         extra_attachments: list | None = None,
                         cc_address: str | list | None = None,
                         max_retries: int = 3) -> bool:
    import time
    to_list = [to_address] if isinstance(to_address, str) else (to_address or [])
    expanded_to = []
    for item in to_list:
        for part in str(item).split(","):
            if part.strip():
                expanded_to.append(part.strip())

    cc_list = []
    if cc_address:
        raw_cc = [cc_address] if isinstance(cc_address, str) else cc_address
        for item in raw_cc:
            for part in str(item).split(","):
                if part.strip():
                    cc_list.append(part.strip())

    to_str = ", ".join(expanded_to)
    cc_str = ", ".join(cc_list) if cc_list else "None"

    if dry_run:
        att_info = f" + PDF ({len(pdf_bytes):,} bytes)" if pdf_bytes else ""
        chain_info = f" + {len(extra_attachments)} chain email(s)" if extra_attachments else ""
        print(f"  [DRY-RUN] Would send email to To={to_str} CC={cc_str}: {subject}{att_info}{chain_info}")
        return True

    url = f"https://graph.microsoft.com/v1.0/users/{mailbox}/sendMail"
    message = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": body_html},
        "toRecipients": [{"emailAddress": {"address": addr}} for addr in expanded_to],
    }
    if cc_list:
        message["ccRecipients"] = [{"emailAddress": {"address": addr}} for addr in cc_list]

    # Attach PO PDF if available
    attachments = []
    if pdf_bytes:
        import base64
        att_name = pdf_filename or "PurchaseOrder.pdf"
        attachments.append({
            "@odata.type":  "#microsoft.graph.fileAttachment",
            "name":         att_name,
            "contentType":  "application/pdf",
            "contentBytes": base64.b64encode(pdf_bytes).decode("utf-8"),
        })
        print(f"  [PDF] Attaching '{att_name}' ({len(pdf_bytes):,} bytes) to email.")

    # Attach chain emails (.eml files) if provided
    if extra_attachments:
        import base64
        for eml_att in extra_attachments:
            attachments.append(eml_att)
        print(f"  [CHAIN] Attaching {len(extra_attachments)} email(s) from thread.")

    if attachments:
        message["attachments"] = attachments

    payload = {"message": message, "saveToSentItems": "true"}
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    # Retry with exponential backoff for transient network errors
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=90)
            if resp.status_code == 202:
                att_ok = f" + PDF" if pdf_bytes else ""
                chain_ok = f" + {len(extra_attachments or [])} chain email(s)" if extra_attachments else ""
                print(f"  [OK] Email sent{att_ok}{chain_ok} to {to_address}: {subject[:60]}")
                return True
            elif resp.status_code == 429:  # Rate limited
                retry_after = int(resp.headers.get('Retry-After', 10))
                print(f"  [RATE-LIMIT] Graph API throttled. Waiting {retry_after}s before retry {attempt}/{max_retries}...")
                time.sleep(retry_after)
            else:
                print(f"  [ERROR] sendMail failed ({resp.status_code}): {resp.text[:200]}")
                return False
        except requests.exceptions.ConnectionError as e:
            wait = 2 ** attempt  # 2s, 4s, 8s
            print(f"  [RETRY {attempt}/{max_retries}] Connection error: {e}. Retrying in {wait}s...")
            time.sleep(wait)
        except Exception as e:
            print(f"  [ERROR] Unexpected error sending email: {e}")
            return False

    print(f"  [ERROR] Failed to send email after {max_retries} retries.")
    return False


def fetch_thread_eml_attachments(
    token: str,
    mailbox: str,
    conversation_id: str,
    max_emails: int = 10,
    max_total_bytes: int = 18 * 1024 * 1024,  # 18 MB guard (sendMail limit is 25 MB incl. base64)
) -> list:
    """
    Fetch all emails in a conversation thread and return them as Graph
    fileAttachment dicts (.eml format) ready to embed in a sendMail payload.

    Args:
        token:           Graph API bearer token.
        mailbox:         Service mailbox address (e.g. salesorders@envalior.com).
        conversation_id: The Graph conversationId of the original PO email thread.
        max_emails:      Stop after this many thread messages (oldest-first).
        max_total_bytes: Stop adding attachments once total raw bytes exceed this.

    Returns:
        List of fileAttachment dicts (may be empty if fetch fails or no thread).
    """
    if not conversation_id or not token or not mailbox:
        return []

    import base64

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    graph_base = "https://graph.microsoft.com/v1.0"

    # ── Step 1: list all messages in the conversation ─────────────────────────
    try:
        list_url = (
            f"{graph_base}/users/{mailbox}/messages"
            f"?$filter=conversationId eq '{conversation_id}'"
            f"&$select=id,subject,from,receivedDateTime"
            f"&$top={max_emails}"
        )
        resp = requests.get(list_url, headers=headers, timeout=30)
        if resp.status_code != 200:
            print(f"  [CHAIN-WARN] Could not list thread messages ({resp.status_code}): {resp.text[:150]}")
            return []
        thread_msgs = resp.json().get("value", [])
        if not thread_msgs:
            print(f"  [CHAIN] No thread messages found for conversationId.")
            return []
        # Sort in Python (oldest first) to avoid MS Graph 400 InefficientFilter error
        thread_msgs.sort(key=lambda m: m.get("receivedDateTime", ""))
        print(f"  [CHAIN] Found {len(thread_msgs)} email(s) in thread — downloading as .eml...")
    except Exception as e:
        print(f"  [CHAIN-WARN] Thread listing failed: {e}")
        return []

    # ── Step 2: download each message as raw MIME (.eml) ─────────────────────
    attachments = []
    total_bytes = 0

    for seq, msg in enumerate(thread_msgs, start=1):
        msg_id   = msg.get("id", "")
        subject  = msg.get("subject", "no_subject") or "no_subject"
        sender   = (msg.get("from", {}) or {}).get("emailAddress", {}).get("address", "unknown")
        recv_dt  = (msg.get("receivedDateTime", "") or "")[:10]  # YYYY-MM-DD

        # Build a safe filename: email_01_2026-07-15_sender@domain.com.msg
        safe_sender = sender.replace("@", "_at_").replace("/", "_")[:40]
        eml_name = f"email_{seq:02d}_{recv_dt}_{safe_sender}.msg"

        try:
            eml_url = f"{graph_base}/users/{mailbox}/messages/{msg_id}/$value"
            eml_headers = dict(headers)
            eml_headers["Accept"] = "application/octet-stream"
            eml_resp = requests.get(eml_url, headers=eml_headers, timeout=30)
            if eml_resp.status_code != 200:
                print(f"  [CHAIN-WARN] Could not download eml #{seq} ({eml_resp.status_code}) — skipping.")
                continue
            eml_bytes = eml_resp.content
        except Exception as e:
            print(f"  [CHAIN-WARN] EML download #{seq} failed: {e} — skipping.")
            continue

        if total_bytes + len(eml_bytes) > max_total_bytes:
            print(f"  [CHAIN-WARN] Size limit reached after {seq - 1} email(s) "
                  f"({total_bytes / 1024 / 1024:.1f} MB) — stopping thread attachment.")
            break

        total_bytes += len(eml_bytes)
        attachments.append({
            "@odata.type":  "#microsoft.graph.fileAttachment",
            "name":         eml_name,
            "contentType":  "message/rfc822",
            "contentBytes": base64.b64encode(eml_bytes).decode("utf-8"),
        })
        print(f"  [CHAIN] Attached: {eml_name} ({len(eml_bytes) / 1024:.1f} KB)")

    print(f"  [CHAIN] Total thread attachments: {len(attachments)} email(s) ({total_bytes / 1024:.1f} KB)")
    return attachments


def _fetch_single_eml(token: str, mailbox: str, message_id: str, subject: str = "") -> list:
    """
    Fetch a single email by message_id as a raw .eml attachment dict.
    Used as fallback for Roborana notifications when conversationId is missing.

    When the email has been moved to a sub-folder (Processed POs / Exception POs),
    the original message_id becomes stale and returns 404. In that case we search
    for the message by subject across all folders and re-fetch by the new ID.

    Returns:
        List with a single fileAttachment dict, or empty list on failure.
    """
    if not token or not mailbox or not message_id:
        return []
    import base64

    def _eml_bytes_from_id(mid: str) -> bytes | None:
        """Fetch raw .eml bytes for a given message ID."""
        url = (f"https://graph.microsoft.com/v1.0/users/{mailbox}"
               f"/messages/{mid}/$value")
        r = requests.get(url,
                         headers={"Authorization": f"Bearer {token}",
                                  "Accept": "application/octet-stream"},
                         timeout=30)
        return r.content if r.status_code == 200 else None

    def _find_by_subject(subj: str) -> str | None:
        """Search all mailbox folders for a message matching this subject."""
        if not subj:
            return None
        # Search in the main mailbox (includes sub-folders with /messages?$search)
        safe_q = subj[:80].replace('"', '')
        search_url = (f"https://graph.microsoft.com/v1.0/users/{mailbox}"
                      f"/messages?$search=%22{requests.utils.quote(safe_q)}%22"
                      f"&$select=id,subject&$top=5")
        r = requests.get(search_url,
                         headers={"Authorization": f"Bearer {token}"},
                         timeout=30)
        if r.status_code == 200:
            for item in r.json().get("value", []):
                if safe_q.lower()[:30] in (item.get("subject") or "").lower():
                    return item["id"]
        return None

    try:
        # ── Attempt 1: direct fetch by stored message_id ──────────────────
        eml_bytes = _eml_bytes_from_id(message_id)

        # ── Attempt 2: message was moved to sub-folder → search by subject ─
        if not eml_bytes and subject:
            print(f"  [EML-WARN] Direct msg_id fetch failed (likely moved folder) — "
                  f"searching by subject: '{subject[:50]}'")
            new_id = _find_by_subject(subject)
            if new_id and new_id != message_id:
                print(f"  [EML-INFO] Found message in current folder, re-fetching...")
                eml_bytes = _eml_bytes_from_id(new_id)

        if not eml_bytes:
            print(f"  [EML-WARN] Could not fetch .eml for message_id={message_id[:30]}...")
            return []

        safe_subj = (subject or "original_email")[:50].replace("/", "_").replace("\\", "_")
        eml_name  = f"original_{safe_subj}.msg"
        print(f"  [MSG] Attached original email as '{eml_name}' ({len(eml_bytes) / 1024:.1f} KB)")
        return [{
            "@odata.type":  "#microsoft.graph.fileAttachment",
            "name":         eml_name,
            "contentType":  "application/vnd.ms-outlook",
            "contentBytes": base64.b64encode(eml_bytes).decode("utf-8"),
        }]
    except Exception as e:
        print(f"  [EML-WARN] Single .eml fetch failed: {e}")
        return []


def move_email_to_exception_folder(token: str, mailbox: str,
                                   message_id: str, dry_run: bool = False) -> bool:
    """Move email to 'Exception POs' folder for Stage 2 SO_CREATION_FAILED cases."""
    if dry_run:
        print(f"  [DRY-RUN] Would move {message_id[:20]}... to Exception POs.")
        return True
    try:
        from setup_mailbox_folders import get_folder_ids, move_email_to_folder
        folders = get_folder_ids(token)
        exception_id = folders.get("exception")
        if not exception_id:
            print("  [WARN] Exception folder ID not found.")
            return False
        return move_email_to_folder(token, message_id, exception_id)
    except Exception as e:
        print(f"  [WARN] Could not move email to Exception POs: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Main Stage 2 Processing Loop
# ─────────────────────────────────────────────────────────────────────────────
def run_stage2_feedback(dry_run: bool = False, filter_po: str = None, hours_back: int = 168, no_time_filter: bool = False, current_run: bool = False):
    """
    Main Stage 2 loop:
      1. Fetch SO results from Celonis
      2. Match each PO result to DB emails with status=PUSHED_TO_CELONIS
      3. Route: Robona (success) / CSR Stage 2 (blocked) / CSR Stage 2 + Exception (failed)
    """
    print("\n" + "=" * 70)
    print("  STAGE 2: Celonis Feedback — SO Creation Results")
    print("=" * 70)
    print(f"  Dry-run mode: {dry_run}")
    print(f"  Celonis table: {SO_RESULTS_TABLE}")
    if no_time_filter:
        print(f"  Time filter: DISABLED (all rows)")
    else:
        print(f"  Hours back: {hours_back}")
    if filter_po:
        print(f"  Filter PO(s): {filter_po}")
    if current_run:
        print(f"  Current Run Only: ENABLED (processing only POs from latest pipeline run)")

    # Parse filter_po into a list of lowercase strings
    filter_po_list = []
    if filter_po:
        filter_po_list = [p.strip().lower() for p in str(filter_po).split(",") if p.strip()]

    # Collect current run PO numbers and files from CSV if current_run requested
    current_run_pos = set()
    current_run_files = set()
    if current_run:
        csv_p = Path("extracted_pos_outlook_po_extracted.csv")
        if csv_p.exists():
            try:
                import pandas as _pdf
                _df_cr = _pdf.read_csv(csv_p)
                if "PO Number" in _df_cr.columns:
                    current_run_pos = {str(x).strip().lower() for x in _df_cr["PO Number"].dropna() if str(x).strip()}
                if "Source File" in _df_cr.columns:
                    current_run_files = {str(x).strip().lower() for x in _df_cr["Source File"].dropna() if str(x).strip()}
                print(f"  [INFO] Loaded {len(current_run_pos)} PO(s) and {len(current_run_files)} file(s) from {csv_p.name}")
            except Exception as e:
                print(f"  [WARN] Could not load current run CSV: {e}")

    # Load config
    regional_config = load_regional_config()
    mailbox = os.getenv("TARGET_EMAIL_USER", "salesorders@envalior.com")
    robona_mailbox = os.getenv("ROBONA_SERVICE_MAILBOX", "robona-test@envalior.com")

    # Fetch SO results — from Azure Blob (primary), Celonis direct (fallback)
    so_results = fetch_so_results_from_azure(hours_back=hours_back, no_time_filter=no_time_filter)
    if not so_results:
        print("  [INFO] No SO results to process. Run the Celonis Action Flow first.")
        return

    # Build lookup: po_number (lowercase) → list of result rows
    po_result_map = {}
    target_pos_filter = set(filter_po_list) | current_run_pos
    for row in so_results:
        po_num = str(row.get("PO_NUMBER", "") or "").strip().lower()
        if target_pos_filter:
            # Match if any filter token is in po_num or vice versa
            matched_f = any(tp and (tp == po_num or tp in po_num or po_num in tp) for tp in target_pos_filter)
            if not matched_f:
                continue
        if po_num:
            po_result_map.setdefault(po_num, []).append(row)

    print(f"  [INFO] {len(po_result_map)} unique PO result(s) from Celonis matching criteria.")

    # Initialize cloud-native tracker (Azure Table Storage — single source of truth)
    from azure_email_tracker import AzureEmailTracker
    tracker = AzureEmailTracker()

    # Detect tracker mode — NEVER fall back to SQLite for Robona sends
    # Falling back to SQLite causes duplicate sends: SQLite never has ROBONA_SENT
    # so all 400+ PENDING_STAGE2 records are re-processed on every run.
    if tracker._sqlite_mode:
        print("  [WARN] Azure Table Storage is NOT reachable — running in SQLite-only mode.")
        print("  [WARN] Robona sends are DISABLED in SQLite-only mode to prevent duplicate emails.")
        print("  [WARN] Fix: ensure AZURE_BLOB_URL and credentials are set, then re-run.")
        return

    print("  [INFO] Azure Table Storage: CONNECTED (primary truth source for ROBONA_SENT status)")

    # Fetch eligible emails — only from Azure Table (never SQLite fallback for sends)
    eligible_statuses = ['PUSHED_TO_CELONIS', 'MAPPED_SUCCESS', 'PENDING_STAGE2', 'FOLDER_MOVED']
    eligible_emails = tracker.get_eligible_emails(eligible_statuses)

    if target_pos_filter or current_run_files:
        filtered_emails = []
        for em in eligible_emails:
            sf = (em.get("source_file") or "").lower()
            sb = (em.get("subject") or "").lower()
            matched = False
            for p in target_pos_filter:
                if p and (p in sf or p in sb):
                    matched = True
                    break
            if not matched and current_run_files:
                for cf in current_run_files:
                    cf_stem = cf.rsplit('.', 1)[0]
                    if cf and (cf in sf or cf_stem in sf or cf in sb or cf_stem in sb):
                        matched = True
                        break
            if matched:
                filtered_emails.append(em)
        print(f"  [INFO] Filtered eligible emails from {len(eligible_emails)} down to {len(filtered_emails)} for target POs / current run.")
        eligible_emails = filtered_emails

    # Build set of SO numbers already sent via Robona (from Azure Table Storage)
    already_sent_so = set()
    # Build set of content hashes already sent via Robona (MD5 of mailbox|so|po)
    # This is the primary hash-based dedup guard — prevents duplicate Robona mails
    # across agent runs even when SO number lookup produces inconsistent results.
    sent_hashes = set()
    try:
        sent_records = tracker.get_eligible_emails(['ROBONA_SENT'])
        for sr in sent_records:
            sn = str(sr.get('so_number', '') or '').strip()
            if sn:
                for s in sn.split(';'):
                    if s.strip():
                        already_sent_so.add(s.strip())
            # Collect stored content hash (set at send time)
            rh = str(sr.get('robona_hash', '') or '').strip()
            if rh:
                sent_hashes.add(rh)
        print(f"  [INFO] {len(already_sent_so)} unique SO number(s) recorded as ROBONA_SENT in Azure Table Storage.")
        print(f"  [INFO] {len(sent_hashes)} Robona content hash(es) loaded from Azure Table Storage.")
    except Exception as e:
        print(f"  [WARN] Could not load ROBONA_SENT records from Azure Table Storage: {e}")

    print(f"  [INFO] {len(eligible_emails)} email(s) eligible for Stage 2 processing.")

    # Graph API token (only needed for non-dry-run)
    graph_token = get_graph_token() if not dry_run else "DRY_RUN_TOKEN"
    folder_token = graph_token  # same token used for folder moves

    # Counters
    robona_sent   = 0
    blocked_sent  = 0
    failed_sent   = 0
    no_match      = 0
    SEND_DELAY    = 0.4  # seconds between sends to avoid Graph API rate limiting

    processed_msg_keys = set()   # dedup by message_id within this run
    processed_so_nums  = set()   # dedup by SO number within this run (strongest guard)
    processed_hashes   = set()   # dedup by content hash within this run
    for email_row in eligible_emails:
        # Support both Azure Table dict and SQLite Row tuple
        msg_id          = email_row.get("message_id") or email_row.get("RowKey", "")
        if msg_id in processed_msg_keys:
            print(f"  [DUP-SKIP] Message already processed this run: {msg_id[:20]}...")
            continue
        processed_msg_keys.add(msg_id)

        status          = email_row.get("status", "")
        source_file     = email_row.get("source_file", "") or ""
        email_subject   = email_row.get("subject", "") or ""
        conversation_id = email_row.get("conversation_id", "") or ""
        # Try to find matching Celonis results by PO number
        matched_rows = None

        # Primary: match source_file stem against PO number from Celonis
        if source_file:
            sf_lower = source_file.lower()
            for po_num, rows in po_result_map.items():
                if po_num and (po_num in sf_lower or sf_lower in po_num):
                    matched_rows = rows
                    break

        # Secondary: match email subject against PO number from Celonis
        if not matched_rows and email_subject:
            subj_lower = email_subject.lower()
            for po_num, rows in po_result_map.items():
                if po_num and po_num in subj_lower:
                    matched_rows = rows
                    break

        # Tertiary: match Celonis SOURCE_FILE against email source_file or subject
        # (Celonis stores the original PDF name — e.g. 'PO-90004898.pdf')
        if not matched_rows:
            for po_num, rows in po_result_map.items():
                for r in rows:
                    celonis_sf = str(r.get("SOURCE_FILE", "") or "").strip().lower()
                    if not celonis_sf:
                        continue
                    celonis_sf_stem = celonis_sf.rsplit('.', 1)[0]  # strip extension
                    if source_file and celonis_sf_stem and celonis_sf_stem in source_file.lower():
                        matched_rows = rows
                        break
                    if email_subject and celonis_sf_stem and celonis_sf_stem in email_subject.lower():
                        matched_rows = rows
                        break
                if matched_rows:
                    break

        # Quaternary: meaningful keyword and token overlap match
        if not matched_rows:
            import re
            STOPWORDS = {
                'pdf', 'xlsx', 'xls', 'txt', 'excel', 'body', 'fw', 're', 'aamkagez', 'aqmkagez',
                'test', 'testing', 'order', 'purchase', 'envalior', 'gmbh', 'corp', 'inc', 'ltd',
                '2026', '2025', 'item', 'item1', 'item2', 'new', 'po', 'pos', 'draft', 'for',
                'bestellung', 'auftrag', 'commande', 'ordine', 'pedido', 'christian', 'moq', 'below'
            }
            KEYWORDS = {'asos', 'gewiss', 'stebro', 'molex', 'foxconn', 'purefishing', 'legrand'}

            e_text = f"{email_subject} {source_file}".lower()
            e_tokens = set(re.findall(r'[a-zA-Z0-9]+', e_text)) - STOPWORDS

            for po_num, rows in po_result_map.items():
                for r in rows:
                    c_sf = str(r.get("SOURCE_FILE", "") or "").strip().lower()
                    c_po = str(r.get("PO_NUMBER", "") or "").strip().lower()
                    c_text = f"{c_sf} {c_po}"
                    
                    is_match = False
                    # Check brand/customer keyword overlap (e.g. 'asos' in both)
                    for kw in KEYWORDS:
                        if kw in e_text and kw in c_text:
                            is_match = True
                            break

                    if not is_match:
                        c_tokens = set(re.findall(r'[a-zA-Z0-9]+', c_text)) - STOPWORDS
                        overlap = e_tokens & c_tokens
                        if any(len(t) >= 6 for t in overlap) or len(overlap) >= 2:
                            is_match = True

                    if is_match:
                        matched_rows = rows
                        print(f"  [MATCH-FUZZY] Matched '{email_subject or source_file}' -> Celonis PO '{r.get('PO_NUMBER')}' (SO: {r.get('SO_NUMBER')})")
                        break
                if matched_rows:
                    break

        if not matched_rows:
            no_match += 1
            print(f"  [NO MATCH] msg={msg_id[:16]}... source={source_file or email_subject or '?'}")
            continue

        # Extract and aggregate result fields from all matched rows
        so_number_list = []
        block_code_list = []
        block_reason_list = []
        failure_reason_list = []
        sales_org = ""
        po_number = ""
        
        # Determine aggregated status
        has_failed = False
        has_blocked = False
        has_success = False

        for row in matched_rows:
            row_status = str(row.get("STATUS", "UNKNOWN")).strip().upper()
            row_so = str(row.get("SO_NUMBER", "") or "").strip()
            row_block = str(row.get("BLOCK_CODE", "") or "").strip()
            row_reason = str(row.get("BLOCK_REASON", "") or "").strip()
            row_fail = str(row.get("FAILURE_REASON", "") or "").strip()
            row_sorg = str(row.get("SALES_ORG", "") or "").strip()
            row_po = str(row.get("PO_NUMBER", "") or "").strip()

            if row_so and row_so not in so_number_list:
                so_number_list.append(row_so)
            if row_block and row_block not in block_code_list:
                block_code_list.append(row_block)
            if row_reason and row_reason not in block_reason_list:
                block_reason_list.append(row_reason)
            if row_fail and row_fail not in failure_reason_list:
                failure_reason_list.append(row_fail)
            if row_sorg and not sales_org:
                sales_org = row_sorg
            if row_po and not po_number:
                po_number = row_po

            if row_status == "FAILED":
                has_failed = True
            elif row_status in ("SUCCESS", "BLOCKED") and row_block:
                has_blocked = True
            elif row_status == "SUCCESS":
                has_success = True

        # Resolve aggregated status
        if has_failed:
            result_status = "FAILED"
        elif has_blocked or block_code_list:
            result_status = "BLOCKED"
        else:
            result_status = "SUCCESS"

        so_number = "; ".join(so_number_list)
        block_code = ", ".join(block_code_list)
        block_reason = "; ".join(block_reason_list) or "Order block placed by SAP"
        failure_reason = "; ".join(failure_reason_list) or "Action Flow error — SO not created"

        from csr_routing import resolve_csr_routing
        to_recipients, cc_recipients, region, tier_desc = resolve_csr_routing(
            sales_org=sales_org,
            customer_number=po_number,
            header_fields={"subject": email_subject, "po_number": po_number},
            regional_config=regional_config
        )

        label = source_file or email_subject or msg_id[:20]
        print(f"\n  Processing: {label}")
        print(f"    SO: {so_number or 'N/A'}  |  Status: {result_status}  |  Region: {region} ({tier_desc})")

        # ── Case A: SUCCESS — SO created, no block ────────────────────────
        if result_status == "SUCCESS" and so_number and not block_code:
            from email_templates import get_robona_template

            # ── SO-level dedup: skip if this SO was already sent (Azure truth OR this run) ──
            so_nums_this_batch = [s.strip() for s in so_number.split(";") if s.strip()]
            already_handled = [
                sn for sn in so_nums_this_batch
                if sn in already_sent_so or sn in processed_so_nums
            ]
            if already_handled:
                print(f"    → [DUP-SKIP] Robona already sent for SO(s): {already_handled} — skipping.")
                continue

            # ── Content hash dedup (secondary guard — hash of mailbox|so|po) ──
            # Prevents duplicate Robona mails when SO number comparison is
            # unreliable (e.g. leading zeros, whitespace, re-extracted PO).
            robona_hash = hashlib.md5(
                f"{robona_mailbox}|{so_number.lower()}|{po_number.lower()}".encode()
            ).hexdigest()
            if robona_hash in sent_hashes or robona_hash in processed_hashes:
                print(f"    → [DUP-SKIP] Robona content hash already sent: {robona_hash[:12]}... "
                      f"(SO: {so_number}) — skipping duplicate.")
                continue
            # Mark as processed for this run
            processed_so_nums.update(so_nums_this_batch)
            processed_hashes.add(robona_hash)

            robona_subject = f"{email_subject or po_number} SO# {so_number}"
            robona_body    = get_robona_template(so_number, msg_id, source_file or "N/A", mailbox)

            # ── Download original PO PDF from Azure Blob ──────────────────────
            pdf_bytes = download_po_pdf(source_file, dry_run=dry_run) if source_file else None
            pdf_name  = source_file if source_file and source_file.endswith(".pdf") else f"PO_{po_number}.pdf"

            # Fallback: fetch PDF directly from the original Graph API email attachment
            if not pdf_bytes and not dry_run and msg_id:
                graph_pdf_bytes, graph_pdf_name = fetch_graph_message_pdf_attachment(graph_token, mailbox, msg_id)
                if graph_pdf_bytes:
                    pdf_bytes = graph_pdf_bytes
                    pdf_name = graph_pdf_name or pdf_name

            # ── Attach original email in .msg format ──────────────────────────
            # Always attach the original PO email as a single .msg file so Robona
            # has the complete email thread in Outlook format (message/rfc822).
            # Primary: fetch by message_id (exact original email).
            # Secondary: fetch full conversation thread if message_id is missing.
            thread_atts = []
            if not dry_run:
                if msg_id:
                    # Fetch the exact original PO email as .msg (preferred — one clean attachment)
                    print(f"  [MSG] Fetching original PO email as .msg for message {msg_id[:20]}...")
                    thread_atts = _fetch_single_eml(graph_token, mailbox, msg_id, email_subject)
                    if not thread_atts and conversation_id:
                        # Fallback: fetch full thread if single message fetch failed
                        print(f"  [CHAIN] Single .msg failed — fetching full thread via conversationId...")
                        thread_atts = fetch_thread_eml_attachments(
                            token=graph_token,
                            mailbox=mailbox,
                            conversation_id=conversation_id,
                        )
                elif conversation_id:
                    print(f"  [CHAIN] No message_id — fetching full thread via conversationId...")
                    thread_atts = fetch_thread_eml_attachments(
                        token=graph_token,
                        mailbox=mailbox,
                        conversation_id=conversation_id,
                    )
            elif msg_id:
                print(f"    [DRY-RUN] Would attach original .msg for message {msg_id[:20]}...")
            elif conversation_id:
                print(f"    [DRY-RUN] Would attach email chain for conversationId={conversation_id[:20]}...")

            print(f"    → [SUCCESS] Sending Robona (SO: {so_number}) | PDF: {'YES' if pdf_bytes else 'NO'} | .msg: {len(thread_atts)}")
            test_email = os.getenv("TEST_CS_EMAIL", "a.rathore@ofiservices.com")
            if send_email_via_graph(graph_token, mailbox, robona_mailbox,
                                    robona_subject, robona_body, dry_run=dry_run,
                                    pdf_bytes=pdf_bytes, pdf_filename=pdf_name,
                                    extra_attachments=thread_atts or None,
                                    cc_address=test_email):
                robona_sent += 1
                if not dry_run:
                    # Update status, so_number, and robona_hash in Azure Table Storage
                    # robona_hash stored so future runs can skip this SO via hash check
                    tracker.update_status(
                        msg_id, 'ROBONA_SENT',
                        extra_fields={'so_number': so_number, 'robona_hash': robona_hash}
                    )
                    # Refresh in-memory dedup sets for this run
                    already_sent_so.update(so_nums_this_batch)
                    sent_hashes.add(robona_hash)

        # ── Case B: BLOCKED — SO created but has an order block ──────────
        elif result_status in ("SUCCESS", "BLOCKED") and block_code:
            from email_templates import get_stage2_so_blocked_template

            # Download PO PDF + original email to attach to CSR block notification
            pdf_bytes_b = download_po_pdf(source_file, dry_run=dry_run) if source_file else None
            pdf_name_b  = source_file if source_file and source_file.endswith(".pdf") else f"PO_{po_number}.pdf"
            if not pdf_bytes_b and not dry_run and msg_id:
                _gb, _gn = fetch_graph_message_pdf_attachment(graph_token, mailbox, msg_id)
                if _gb:
                    pdf_bytes_b, pdf_name_b = _gb, (_gn or pdf_name_b)
            blocked_thread_atts = []
            if not dry_run and msg_id:
                print(f"  [MSG] Fetching original .msg for BLOCKED CSR email...")
                blocked_thread_atts = _fetch_single_eml(graph_token, mailbox, msg_id, email_subject)
                if not blocked_thread_atts and conversation_id:
                    blocked_thread_atts = fetch_thread_eml_attachments(
                        graph_token, mailbox, conversation_id
                    )

            # Build all attachments for CSR: PDF + original .msg
            _blocked_atts = []
            if pdf_bytes_b:
                import base64 as _b64
                _blocked_atts.append({
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": pdf_name_b,
                    "contentType": "application/pdf",
                    "contentBytes": _b64.b64encode(pdf_bytes_b).decode("utf-8"),
                })
            _blocked_atts.extend(blocked_thread_atts)

            subject = f"[SAP Order Block - {region}] SO {so_number} — {block_reason[:40]}"
            body    = get_stage2_so_blocked_template(
                region, po_number, so_number, block_code, block_reason
            )
            print(f"    → [BLOCKED] Sending Stage 2 CSR email "
                  f"(block: {block_code}) | PDF: {'YES' if pdf_bytes_b else 'NO'} "
                  f"| .msg: {len(blocked_thread_atts)}")

            # Send CSR block email WITH PO PDF + .msg attached
            _block_payload_atts = _blocked_atts if _blocked_atts else None
            if not dry_run and _block_payload_atts:
                # Inject into graph call via extra_attachments using pdf_bytes slot
                # send_email_via_graph handles pdf_bytes separately so use extra_attachments
                import base64 as _b64b
                _csr_b_result = send_email_via_graph(
                    graph_token, mailbox, to_recipients,
                    subject, body, dry_run=dry_run, cc_address=cc_recipients,
                    pdf_bytes=pdf_bytes_b, pdf_filename=pdf_name_b,
                    extra_attachments=blocked_thread_atts or None,
                )
            else:
                _csr_b_result = send_email_via_graph(
                    graph_token, mailbox, to_recipients,
                    subject, body, dry_run=dry_run, cc_address=cc_recipients,
                )
            if _csr_b_result:
                blocked_sent += 1
                if not dry_run:
                    tracker.update_status(msg_id, 'SO_BLOCKED')

            # Also send Robona even for blocked SO (SO exists, it just needs release)
            # Attach original PO PDF + original .msg so Robona has full context.
            from email_templates import get_robona_template
            robona_hash = hashlib.md5(
                f"{robona_mailbox}|{so_number.lower()}|{po_number.lower()}".encode()
            ).hexdigest()
            robona_subject = f"{email_subject or po_number} SO# {so_number} [BLOCKED]"
            robona_body    = get_robona_template(so_number, msg_id, source_file or "N/A", mailbox)
            test_email = os.getenv("TEST_CS_EMAIL", "a.rathore@ofiservices.com")
            send_email_via_graph(graph_token, mailbox, robona_mailbox,
                                 robona_subject, robona_body, dry_run=dry_run,
                                 pdf_bytes=pdf_bytes_b, pdf_filename=pdf_name_b,
                                 extra_attachments=blocked_thread_atts or None,
                                 cc_address=test_email)

        # ── Case C: FAILED — SO was never created ────────────────────────
        elif result_status == "FAILED":
            from email_templates import get_stage2_so_failed_template

            # Extract exact RFC error fields from matched Celonis rows
            # These come from the BAPI_SALESORDER_CREATEFROMDAT2 RETURN table
            rfc_error_code_val = ""
            rfc_message_val    = ""
            for _row in matched_rows:
                _code = str(_row.get("RFC_ERROR_CODE", "") or "").strip()
                _msg  = str(_row.get("RFC_MESSAGE", "") or "").strip()
                if _code and not rfc_error_code_val:
                    rfc_error_code_val = _code
                if _msg and not rfc_message_val:
                    rfc_message_val = _msg

            # Build subject with RFC error code when available
            if rfc_error_code_val:
                subject = (f"[Stage 2 Exception - {region}] SAP RFC Error "
                           f"[{rfc_error_code_val}] — {po_number}")
            else:
                subject = f"[Stage 2 Exception - {region}] SAP SO Creation Failed — {po_number}"

            body = get_stage2_so_failed_template(
                region, po_number, failure_reason,
                rfc_error_code=rfc_error_code_val,
                rfc_message=rfc_message_val,
            )

            # ── Attach PO PDF + original .msg to FAILED CSR notification ─────
            # CSR must receive the PO they need to manually create in SAP.
            pdf_bytes_f = download_po_pdf(source_file, dry_run=dry_run) if source_file else None
            pdf_name_f  = source_file if source_file and source_file.endswith(".pdf") else f"PO_{po_number}.pdf"
            if not pdf_bytes_f and not dry_run and msg_id:
                _gf_b, _gf_n = fetch_graph_message_pdf_attachment(graph_token, mailbox, msg_id)
                if _gf_b:
                    pdf_bytes_f, pdf_name_f = _gf_b, (_gf_n or pdf_name_f)

            failed_thread_atts = []
            if not dry_run and msg_id:
                print(f"  [MSG] Fetching original .msg for FAILED CSR email...")
                failed_thread_atts = _fetch_single_eml(graph_token, mailbox, msg_id, email_subject)
                if not failed_thread_atts and conversation_id:
                    failed_thread_atts = fetch_thread_eml_attachments(
                        graph_token, mailbox, conversation_id
                    )
            elif dry_run:
                print(f"    [DRY-RUN] Would attach PO PDF: {'YES' if source_file else 'NO'} "
                      f"+ original .msg for FAILED email.")

            print(f"    → [FAILED] Sending Stage 2 CSR email + moving to Exception POs | "
                  f"PDF: {'YES' if pdf_bytes_f else 'NO'} | .msg: {len(failed_thread_atts)}")
            if rfc_error_code_val or rfc_message_val:
                print(f"    → [RFC ERROR] Code: {rfc_error_code_val or 'N/A'} | "
                      f"Message: {(rfc_message_val or 'N/A')[:80]}")

            if send_email_via_graph(graph_token, mailbox, to_recipients,
                                    subject, body, dry_run=dry_run, cc_address=cc_recipients,
                                    pdf_bytes=pdf_bytes_f, pdf_filename=pdf_name_f,
                                    extra_attachments=failed_thread_atts or None):
                failed_sent += 1

            # Move email to Exception POs (SO doesn't exist — needs manual handling)
            move_email_to_exception_folder(folder_token, mailbox, msg_id, dry_run=dry_run)

            if not dry_run:
                tracker.update_status(msg_id, 'SO_CREATION_FAILED')

        else:
            print(f"    → [SKIP] Unknown result status: {result_status} — no action taken")

        # Small inter-email delay to avoid Graph API rate limiting
        import time
        time.sleep(SEND_DELAY)

    if dry_run:
        print("  [DRY-RUN] No DB changes made — running in read-only mode.")

    print(f"\n{'=' * 70}")
    print("  Stage 2 Summary:")
    print(f"     Robona sent         : {robona_sent} (SO created successfully)")
    print(f"     Blocked CSR emails  : {blocked_sent} (SO created but blocked)")
    print(f"     Failed CSR emails   : {failed_sent} (SO creation failed — moved to Exception POs)")
    print(f"     No Celonis match    : {no_match} (not in Celonis results yet)")
    print("=" * 70)
    print("  NOTE: Emails with 'No Celonis match' are still in PUSHED_TO_CELONIS status.")
    print("        Re-run this script after the Celonis Action Flow has completed.")
    print("=" * 70 + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Stage 2 Celonis Feedback — Process SO creation results and notify CSR / Robona"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log actions only — do not call Graph API or modify DB"
    )
    parser.add_argument(
        "--po-number",
        type=str,
        default=None,
        help="Process a single PO number only (for testing)"
    )
    parser.add_argument(
        "--table",
        type=str,
        default=None,
        help="Override the Celonis SO results table name"
    )
    parser.add_argument(
        "--hours-back",
        type=int,
        default=168,
        help="Fetch results from the last N hours (default: 168 = 7 days). Use 0 to disable."
    )
    parser.add_argument(
        "--no-time-filter",
        action="store_true",
        help="Load ALL SO results from blob/Celonis regardless of timestamp (manual catch-up mode)"
    )
    parser.add_argument(
        "--current-run",
        action="store_true",
        help="Process ONLY POs/emails from the latest pipeline run (extracted_pos_outlook_po_extracted.csv)"
    )
    args = parser.parse_args()

    if args.table:
        SO_RESULTS_TABLE = args.table

    run_stage2_feedback(
        dry_run=args.dry_run,
        filter_po=args.po_number,
        hours_back=args.hours_back,
        no_time_filter=args.no_time_filter,
        current_run=args.current_run,
    )
