"""
Graph Email Handler — Post-Processing Module
----------------------------------------------
Provides 3 core functions using Microsoft Graph API:
  1. archive_email()     — Move processed email to /Archive folder
  2. send_exception_email() — Forward failed PO to regional CS inbox
  3. send_robona_notification() — Notify Robona RPA to attach email to SAP SO

Auth: MSAL ConfidentialClientApplication (Client Credentials Flow)

Usage:
  from graph_email_handler import GraphEmailHandler
  handler = GraphEmailHandler()
  handler.archive_email(message_id)
"""

import os
import json
import requests
from typing import Optional, Dict
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# MSAL Authentication
# ---------------------------------------------------------------------------
try:
    import msal
    MSAL_AVAILABLE = True
except ImportError:
    MSAL_AVAILABLE = False
    print("⚠️  WARNING: 'msal' package not installed. Run: pip install msal")


class GraphEmailHandler:
    """
    Microsoft Graph API handler for email archiving, exception routing,
    and Robona RPA notification.
    """

    GRAPH_BASE = "https://graph.microsoft.com/v1.0"

    def __init__(self):
        self.client_id = os.getenv("MS_GRAPH_CLIENT_ID")
        self.tenant_id = os.getenv("MS_GRAPH_TENANT_ID")
        self.client_secret = os.getenv("MS_GRAPH_CLIENT_SECRET")
        self.mailbox = os.getenv("TARGET_EMAIL_USER", "salesorders@envalior.com")
        self.robona_mailbox = os.getenv("ROBONA_SERVICE_MAILBOX", "robona-test@envalior.com")
        self.azure_blob_url = os.getenv("AZURE_BLOB_URL", "https://poextstorage49245.blob.core.windows.net")
        self.po_container   = "input-po"  # container where PO PDFs are stored

        # Load regional config
        config_path = os.path.join(os.path.dirname(__file__), "regional_config.json")
        if os.path.exists(config_path):
            with open(config_path, "r") as f:
                self.regional_config = json.load(f)
        else:
            self.regional_config = {}

        self._access_token = None

    # ------------------------------------------------------------------
    # AUTH
    # ------------------------------------------------------------------
    def _get_token(self) -> str:
        """Acquire an access token using MSAL Client Credentials."""
        if self._access_token:
            return self._access_token

        if not MSAL_AVAILABLE:
            raise RuntimeError("msal package is required. pip install msal")

        app = msal.ConfidentialClientApplication(
            self.client_id,
            authority=f"https://login.microsoftonline.com/{self.tenant_id}",
            client_credential=self.client_secret,
        )

        result = app.acquire_token_for_client(
            scopes=["https://graph.microsoft.com/.default"]
        )

        if "access_token" in result:
            self._access_token = result["access_token"]
            return self._access_token
        else:
            error = result.get("error_description", result.get("error", "Unknown"))
            raise RuntimeError(f"Failed to acquire Graph token: {error}")

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # 0. DOWNLOAD PO PDF FROM AZURE BLOB
    # ------------------------------------------------------------------
    def _download_po_pdf(self, source_file: str) -> bytes | None:
        """
        Download the PO PDF for attaching to the Robona notification email.

        Resolution order:
          1. Azure SDK with connection string (AZURE_STORAGE_CONNECTION_STRING env var)
          2. Azure SDK with DefaultAzureCredential (works in Azure cloud / managed identity)
          3. Local file — outlook_po_archive/ folder  (pipeline archives PDFs here after processing)
          4. Local file — outlook_po_attachments/ folder (active download folder)
          5. Local file — current working directory
          6. Direct HTTP GET (only works if blob container is public)

        Returns raw PDF bytes, or None if all methods fail.
        """
        if not source_file or source_file in ("nan", "None", ""):
            print("  [PDF-WARN] source_file is empty — cannot attach PDF to Robona email.")
            return None

        # ── Method 1: Azure SDK with connection string ────────────────────────
        conn_str = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "")
        if conn_str:
            try:
                from azure.storage.blob import BlobServiceClient
                bsc  = BlobServiceClient.from_connection_string(conn_str)
                data = bsc.get_blob_client(container=self.po_container, blob=source_file).download_blob().readall()
                print(f"  [PDF] Downloaded '{source_file}' ({len(data):,} bytes) via connection string.")
                return data
            except Exception as e:
                print(f"  [PDF-WARN] Connection string download failed: {e}")

        # ── Method 2: Azure SDK with DefaultAzureCredential ──────────────────
        try:
            import logging
            for noisy_logger in ("azure.identity", "azure.identity._credentials",
                                 "azure.core.pipeline.policies.http_logging_policy"):
                logging.getLogger(noisy_logger).setLevel(logging.CRITICAL)
            from azure.storage.blob import BlobServiceClient
            from azure.identity import DefaultAzureCredential
            bsc  = BlobServiceClient(account_url=self.azure_blob_url, credential=DefaultAzureCredential())
            data = bsc.get_blob_client(container=self.po_container, blob=source_file).download_blob().readall()
            print(f"  [PDF] Downloaded '{source_file}' ({len(data):,} bytes) from Azure Blob.")
            return data
        except Exception as e:
            print(f"  [PDF-WARN] Azure DefaultAzureCredential failed: {type(e).__name__}. Trying local fallback...")

        # ── Method 3 & 4 & 5: Local file fallback ─────────────────────────────
        # The pipeline saves PDFs locally before uploading to Azure Blob.
        script_dir   = os.path.dirname(os.path.abspath(__file__))
        local_search = [
            os.path.join(script_dir, "outlook_po_archive",      source_file),
            os.path.join(script_dir, "outlook_po_attachments",  source_file),
            os.path.join(script_dir,                            source_file),
            # Case-insensitive: try lowercase filename too
            os.path.join(script_dir, "outlook_po_archive",      source_file.lower()),
            os.path.join(script_dir, "outlook_po_attachments",  source_file.lower()),
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

        # ── Method 6: Direct HTTP (only works for public blobs) ───────────────
        blob_url = f"{self.azure_blob_url}/{self.po_container}/{source_file}"
        try:
            r = requests.get(blob_url, timeout=30)
            if r.status_code == 200:
                print(f"  [PDF] Downloaded via HTTP: {len(r.content):,} bytes")
                return r.content
            else:
                print(f"  [PDF-WARN] HTTP GET returned {r.status_code} for {blob_url}")
        except Exception as e:
            print(f"  [PDF-WARN] HTTP fallback failed: {e}")

        print(f"  [PDF-WARN] All download methods failed for '{source_file}' — Robona email sent WITHOUT PDF attachment.")
        return None

    # ------------------------------------------------------------------
    # 1. ARCHIVE EMAIL
    # ------------------------------------------------------------------
    def archive_email(self, message_id: str, dry_run: bool = False) -> bool:
        """
        Moves a message from Inbox to the 'Archive' well-known folder.
        
        Args:
            message_id: The Graph API message ID
            dry_run: If True, log the action but don't call the API
            
        Returns:
            True on success
        """
        print(f"[ARCHIVE] Archiving email: {message_id[:20]}...")

        if dry_run:
            print(f"  [DRY-RUN] Would move message {message_id[:20]} to Archive.")
            return True

        url = f"{self.GRAPH_BASE}/users/{self.mailbox}/messages/{message_id}/move"
        payload = {"destinationId": "archive"}

        resp = requests.post(url, headers=self._headers(), json=payload)

        if resp.status_code in (200, 201):
            print(f"  [OK] Email archived successfully.")
            return True
        else:
            print(f"  [ERROR] Archive failed ({resp.status_code}): {resp.text[:200]}")
            return False

    # ------------------------------------------------------------------
    # 2. SEND EXCEPTION EMAIL TO REGIONAL CS
    # ------------------------------------------------------------------
    def _resolve_region(self, sales_org: Optional[str]) -> Dict:
        """
        Determines the regional CS mailbox based on Sales Org code.
        Falls back to EMEA if no match.
        """
        if sales_org:
            for region, config in self.regional_config.items():
                if sales_org in config.get("sales_orgs", []):
                    return {"region": region, **config}

        # Default fallback
        fallback = self.regional_config.get("DEFAULT", {
            "cs_email": os.getenv("DEFAULT_CS_EMAIL", "cs-test@envalior.com"),
            "region": "DEFAULT"
        })
        return {"region": "DEFAULT", **fallback}

    def _get_csr_info(self, customer_number: Optional[str]) -> tuple:
        """
        Looks up the salesperson (employee responsible) name and email
        for a given customer in the Test MP parquet cache.
        Returns: (salesperson_name, salesperson_email)
        """
        if not customer_number:
            return None, None
        try:
            import pandas as pd
            cache_path = os.path.join(".celonis_cache", "test_mp_customer_master.parquet")
            if not os.path.exists(cache_path):
                return None, None
            df = pd.read_parquet(cache_path)
            cust_col = "Customer" if "Customer" in df.columns else "sold_to_id"
            cust_df = df[df[cust_col] == str(customer_number).strip()]
            if not cust_df.empty:
                # Find the first row with non-empty salesperson_email
                for _, row in cust_df.iterrows():
                    email = str(row.get("salesperson_email", "") or "").strip()
                    name = str(row.get("salesperson_name", "") or "").strip()
                    if email and "@" in email:
                        return name, email
                # Fallback to name-only if email not found
                for _, row in cust_df.iterrows():
                    name = str(row.get("salesperson_name", "") or "").strip()
                    if name:
                        return name, None
        except Exception as e:
            print(f"  [WARN] Failed to look up customer CSR/salesperson info: {e}")
        return None, None

    def send_exception_email(
        self,
        message_id: str,
        failure_reason: str,
        sales_org: Optional[str] = None,
        original_filename: Optional[str] = None,
        file_path: Optional[str] = None,
        dry_run: bool = False,
        header_fields: Optional[dict] = None,
        status: Optional[str] = None,
        missing_materials: Optional[list] = None,
    ) -> bool:
        """
        Forwards the failed PO to the appropriate CSR (at customer or regional level).
        
        Args:
            message_id: Original email's Graph API message ID
            failure_reason: Why the AI could not process this PO
            sales_org: Extracted Sales Org code (used for regional routing)
            original_filename: Name of the PDF that failed
            file_path: Local path to the PDF to attach
            dry_run: If True, log but don't call
            header_fields: Dictionary of extracted header details
            status: Status code (e.g. MAPPING_FAILED, EXTRACTION_FAILED)
            missing_materials: List of unmapped customer material codes/descriptions
            
        Returns:
            True on success
        """
        # Resolve customer-level CSR info (Employee Responsible)
        customer_number = None
        if header_fields:
            customer_number = (
                header_fields.get("customer_number")
                or header_fields.get("sold_to_id")
            )
            if not customer_number and header_fields.get("validation"):
                customer_number = header_fields.get("validation", {}).get("matched_sold_to_id")

        csr_name, csr_email = self._get_csr_info(customer_number)

        # 3-Tier CSR Routing Logic
        from csr_routing import resolve_csr_routing
        to_recipients, cc_recipients, region, tier_desc = resolve_csr_routing(
            sales_org=sales_org,
            customer_number=customer_number,
            salesperson_email=csr_email,
            header_fields=header_fields,
            text_content=failure_reason,
            regional_config=self.regional_config,
        )

        to_str = ", ".join(to_recipients)
        cc_str = ", ".join(cc_recipients) if cc_recipients else "None"
        print(f"[EXCEPTION] Exception Routing ({tier_desc}):")
        print(f"   To: {to_str}")
        print(f"   CC: {cc_str}")
        print(f"   Customer CSR: {csr_name or 'N/A'} ({csr_email or 'N/A'})")
        print(f"   Reason: {failure_reason}")

        attachments = []
        if file_path:
            import base64
            from pathlib import Path
            
            # Convert file_path to list if it is not already a list/tuple
            if isinstance(file_path, (str, Path)):
                paths = [file_path]
            elif isinstance(file_path, (list, tuple)):
                paths = file_path
            else:
                paths = [file_path]
                
            for path_item in paths:
                if not path_item:
                    continue
                p = Path(path_item)
                if p.exists():
                    try:
                        with open(p, "rb") as f:
                            file_bytes = f.read()
                        base64_content = base64.b64encode(file_bytes).decode('utf-8')

                        # Explicit MIME map — Python's mimetypes module does not
                        # know about .msg files, so guess_type() returns None and
                        # the fallback application/octet-stream causes corporate
                        # mail gateways to block or strip the attachment.
                        _MIME_OVERRIDE = {
                            ".msg":  "application/vnd.ms-outlook",
                            ".eml":  "message/rfc822",
                            ".pdf":  "application/pdf",
                            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            ".xls":  "application/vnd.ms-excel",
                            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                            ".doc":  "application/msword",
                            ".png":  "image/png",
                            ".jpg":  "image/jpeg",
                            ".jpeg": "image/jpeg",
                        }
                        import mimetypes
                        ext = p.suffix.lower()
                        mime_type = _MIME_OVERRIDE.get(ext)
                        if not mime_type:
                            mime_type, _ = mimetypes.guess_type(str(p))
                        if not mime_type:
                            mime_type = "application/octet-stream"

                        attachments.append({
                            "@odata.type": "#microsoft.graph.fileAttachment",
                            "name": p.name,
                            "contentType": mime_type,
                            "contentBytes": base64_content
                        })
                        print(f"  -> Added email attachment: {p.name} ({len(file_bytes)/1024:.1f} KB, {mime_type})")
                    except Exception as e:
                        print(f"  [WARN] Failed to read attachment file {path_item}: {e}")
                else:
                    print(f"  [WARN] Attachment file does not exist: {path_item}")

        from email_templates import get_exception_template
        body = get_exception_template(
            region,
            original_filename or 'N/A',
            failure_reason,
            message_id,
            header_fields=header_fields,
            status=status,
            missing_materials=missing_materials,
            csr_name=csr_name
        )

        if dry_run:
            print(f"  [DRY-RUN] Would send exception email to To={to_str} CC={cc_str} (attachments: {len(attachments)}).")
            # For testing/logging in dry-run, we can print the dynamic recommendation title
            rec_title = "Manual Action"
            if status == "EXTRACTION_FAILED":
                rec_title = "Check Purchase Order PDF / Create Manually"
            elif status == "MAPPING_FAILED":
                if missing_materials:
                    rec_title = "Update Material Master Data in SAP"
                else:
                    rec_title = "Update Customer Master Data in SAP"
            print(f"  [DRY-RUN] Recommendation: {rec_title}")
            return True

        subject = f"[PO Exception - {region}] AI Processing Failed — {original_filename or 'Unknown File'}"

        url = f"{self.GRAPH_BASE}/users/{self.mailbox}/sendMail"
        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "HTML", "content": body},
                "toRecipients": [
                    {"emailAddress": {"address": email}} for email in to_recipients
                ],
            },
            "saveToSentItems": "true",
        }
        if cc_recipients:
            payload["message"]["ccRecipients"] = [
                {"emailAddress": {"address": email}} for email in cc_recipients
            ]
        if attachments:
            payload["message"]["attachments"] = attachments

        resp = requests.post(url, headers=self._headers(), json=payload)

        if resp.status_code == 202:
            print(f"  [OK] Exception email sent successfully to To={to_str} CC={cc_str}.")
            return True
        else:
            print(f"  [ERROR] sendMail failed ({resp.status_code}): {resp.text[:200]}")
            return False

    # ------------------------------------------------------------------
    # 3. ROBONA RPA NOTIFICATION
    # ------------------------------------------------------------------
    def send_robona_notification(
        self,
        message_id: str,
        so_numbers: "list | str",
        original_filename: Optional[str] = None,
        po_subject: Optional[str] = None,
        source_file: Optional[str] = None,
        dry_run: bool = False,
    ) -> bool:
        """
        Sends a structured notification to Robona's service mailbox.

        Two-step delivery:
          1. Notification email  — subject ends with "SO# {numbers}" so Robona can
             parse the SAP SO reference.  A copy of the PO PDF is attached here
             as a convenience / audit trail.
          2. Forward of the original customer email — the full email body
             (including any free-text comments written by the customer) plus the
             original PDF attachment are forwarded to Robona so nothing is lost.

        Subject line format (agreed standard):
            {original_PO_subject} SO# {SO1}; {SO2}; ...

        Args:
            message_id:        Graph API message ID of the original PO email
            so_numbers:        One SAP SO number (str) or multiple (list of str)
            original_filename: Source PDF filename (display name)
            po_subject:        Subject line of the original incoming PO email
            source_file:       Azure Blob name for PO PDF (used to fetch attachment)
            dry_run:           If True, log but don't call

        Returns:
            True on success (notification step succeeded; forward failure is non-fatal)
        """
        # Normalise SO numbers
        if isinstance(so_numbers, str):
            so_list = [so_numbers]
        else:
            so_list = list(so_numbers)
        so_part = "; ".join(str(s) for s in so_list if s)

        base_subject = (po_subject or original_filename or "Purchase Order").strip()
        subject = f"{base_subject} SO# {so_part}"

        print(f"[ROBONA] Robona Notification -> SO(s): {so_part}")
        print(f"  Subject: {subject}")

        # Download PO PDF for attachment
        blob_name = source_file or original_filename
        pdf_bytes = self._download_po_pdf(blob_name) if blob_name else None
        att_name  = (blob_name if blob_name and blob_name.endswith(".pdf")
                     else f"PO_{so_part.replace('; ', '_')}.pdf")

        if dry_run:
            att_info = f" + PDF ({len(pdf_bytes):,} bytes)" if pdf_bytes else " (no PDF attachment)"
            print(f"  [DRY-RUN] Step 1 — Would notify Robona at {self.robona_mailbox}{att_info}.")
            print(f"  [DRY-RUN] Step 2 — Would forward original customer email (message_id={message_id[:20]}...) to {self.robona_mailbox}.")
            return True

        from email_templates import get_robona_template
        body = get_robona_template(so_part, message_id, original_filename or 'N/A', self.mailbox)

        message = {
            "subject": subject,
            "body": {"contentType": "HTML", "content": body},
            "toRecipients": [{"emailAddress": {"address": self.robona_mailbox}}],
        }

        # Attach PO PDF
        if pdf_bytes:
            import base64
            message["attachments"] = [{
                "@odata.type":  "#microsoft.graph.fileAttachment",
                "name":         att_name,
                "contentType":  "application/pdf",
                "contentBytes": base64.b64encode(pdf_bytes).decode("utf-8"),
            }]
            print(f"  [PDF] Attaching '{att_name}' ({len(pdf_bytes):,} bytes) to Robona notification email.")
        else:
            print(f"  [PDF-WARN] No PDF attachment — Robona notification sent without PDF.")

        url = f"{self.GRAPH_BASE}/users/{self.mailbox}/sendMail"
        payload = {"message": message, "saveToSentItems": "true"}

        resp = requests.post(url, headers=self._headers(), json=payload, timeout=60)

        if resp.status_code == 202:
            att_ok = " + PDF attached" if pdf_bytes else ""
            print(f"  [OK] Step 1 complete — Robona notified{att_ok}. Subject: {subject}")
        else:
            print(f"  [ERROR] Robona notification failed ({resp.status_code}): {resp.text[:200]}")
            return False

        # ── Step 2: Forward the full original customer email to Robona ────────
        # This ensures the customer's email body (free-text comments, instructions,
        # delivery notes etc.) and the original attachment are preserved for Robona.
        forward_comment = (
            f"[Touchless Agent] Original customer PO email for reference. "
            f"SAP Sales Order(s) created: {so_part}. "
            f"Please attach this email to the Sales Order in SAP."
        )
        print(f"  [ROBONA] Step 2 — Forwarding original customer email to Robona ...")
        fwd_ok = self.forward_original_email(
            message_id=message_id,
            to_address=self.robona_mailbox,
            comment=forward_comment,
            dry_run=dry_run,
        )
        if not fwd_ok:
            print(
                f"  [WARN] Step 2 — Forward of original customer email to Robona failed. "
                f"Notification (Step 1) was already sent successfully — SO link is preserved."
            )
        else:
            print(f"  [OK] Step 2 complete — Original customer email forwarded to Robona.")

        # Step 1 succeeded — return True regardless of forward result
        return True

    # ------------------------------------------------------------------
    # 4. FORWARD ORIGINAL EMAIL TO CSR / ROBONA
    # ------------------------------------------------------------------
    def forward_original_email(
        self,
        message_id: str,
        to_address: str,
        comment: str = "",
        dry_run: bool = False,
    ) -> bool:
        """
        Forwards the original incoming PO email (via Graph API /forward)
        to the given address.

        Used in two flows:
          - Exception routing: CSR receives the full customer email thread
          - Robona notification: Robona receives the original email so that
            any free-text comments from the customer are not lost.

        Args:
            message_id: Graph API message ID of the original email
            to_address:  Recipient (e.g. regional CS inbox)
            comment:     Optional introductory comment prepended to the forwarded body
            dry_run:     If True, log but don't call

        Returns:
            True on success
        """
        print(f"[FORWARD] Forwarding original email to {to_address} ...")

        if dry_run:
            print(f"  [DRY-RUN] Would forward message {message_id[:20]} to {to_address}.")
            return True

        url = f"{self.GRAPH_BASE}/users/{self.mailbox}/messages/{message_id}/forward"
        payload = {
            "comment": comment or "Please see the forwarded purchase order below.",
            "toRecipients": [
                {"emailAddress": {"address": to_address}}
            ]
        }

        resp = requests.post(url, headers=self._headers(), json=payload)

        if resp.status_code == 202:
            print(f"  [OK] Original email forwarded to {to_address}.")
            return True
        else:
            print(f"  [WARN] Forward failed ({resp.status_code}): {resp.text[:200]}")
            # Non-fatal: the exception email with PDF attachment is still sent separately
            return False


# ---------------------------------------------------------------------------
# STANDALONE TEST
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Test Graph Email Handler")
    parser.add_argument("--action", choices=["archive", "exception", "robona"], required=True)
    parser.add_argument("--message-id", type=str, default="TEST_MSG_ID_12345")
    parser.add_argument("--so-number", type=str, default="SO-000001")
    parser.add_argument("--reason", type=str, default="Test exception: missing vendor mapping")
    parser.add_argument("--dry-run", action="store_true", default=True)
    args = parser.parse_args()

    handler = GraphEmailHandler()

    if args.action == "archive":
        handler.archive_email(args.message_id, dry_run=args.dry_run)
    elif args.action == "exception":
        handler.send_exception_email(args.message_id, args.reason, dry_run=args.dry_run)
    elif args.action == "robona":
        handler.send_robona_notification(args.message_id, args.so_number, dry_run=args.dry_run)
