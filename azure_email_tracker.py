"""
azure_email_tracker.py
======================
Cloud-native drop-in replacement for the local processed_emails.db SQLite tracker.

Stores email processing state in Azure Table Storage — the same Azure Storage Account
used for Blob Storage (input-po, celonis-tables containers).  No separate resource
needed; just a new table inside the existing account.

Table layout
------------
  Table name   : processedemails
  PartitionKey : "emails"   (single partition — simple, all rows co-located)
  RowKey       : message_id (Graph API message ID — globally unique)
  Properties   : subject, received_date, status, source_file, error_log,
                 so_number, block_code, block_reason, stage,
                 po_number, conversation_id, created_at, updated_at

Auth
----
  DefaultAzureCredential — works locally via 'az login' and on Azure via
  Managed Identity / Service Principal without any extra config.

Fallback
--------
  If Azure Table is unreachable (no credentials, no internet in local dev),
  falls back to the local SQLite processed_emails.db automatically.
  Set AZURE_TABLE_FALLBACK_SQLITE=false in .env to disable the fallback.

Usage
-----
  from azure_email_tracker import AzureEmailTracker
  tracker = AzureEmailTracker()
  tracker.upsert_email(message_id, subject=subject, status='PENDING',
                       conversation_id=conv_id)
  entity  = tracker.get_email(message_id)
  rows    = tracker.get_eligible_emails(['PUSHED_TO_CELONIS', 'MAPPED_SUCCESS'])
  tracker.update_status(message_id, 'ROBONA_SENT')
"""

import os
import sys
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ─── Configuration ────────────────────────────────────────────────────────────
TABLE_NAME      = os.getenv("AZURE_EMAIL_TABLE_NAME", "processedemails")
PARTITION_KEY   = "emails"
FALLBACK_SQLITE = os.getenv("AZURE_TABLE_FALLBACK_SQLITE", "true").lower() != "false"
SQLITE_PATH     = Path(__file__).parent / "processed_emails.db"


def _get_account_name() -> str:
    """Parse storage account name from AZURE_BLOB_URL or AZURE_STORAGE_ACCOUNT_NAME."""
    name = os.getenv("AZURE_STORAGE_ACCOUNT_NAME", "")
    if name:
        return name
    blob_url = os.getenv("AZURE_BLOB_URL", "")
    # e.g. https://poextstorage49245.blob.core.windows.net
    if "blob.core.windows.net" in blob_url:
        return blob_url.split("//")[-1].split(".")[0]
    return ""


def _table_endpoint(account_name: str) -> str:
    return f"https://{account_name}.table.core.windows.net"


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ─── Main class ───────────────────────────────────────────────────────────────

class AzureEmailTracker:
    """
    Azure Table Storage-backed email tracking store.

    Mirrors the interface previously provided by processed_emails.db (SQLite),
    so callers only need minimal changes — replace cursor/conn calls with tracker
    method calls.
    """

    def __init__(self, force_sqlite: bool = False):
        """
        Args:
            force_sqlite: If True, always use local SQLite regardless of Azure availability.
                          Useful for unit tests or fully offline environments.
        """
        self._azure_client = None
        self._sqlite_mode  = force_sqlite

        if not force_sqlite:
            self._azure_client = self._init_azure()
            if self._azure_client is None:
                if FALLBACK_SQLITE:
                    print("  [Tracker] Azure Table unavailable — falling back to local SQLite.")
                    self._sqlite_mode = True
                else:
                    raise RuntimeError(
                        "Azure Table Storage is required (AZURE_TABLE_FALLBACK_SQLITE=false) "
                        "but could not be initialised. Check credentials and AZURE_BLOB_URL."
                    )

        if self._sqlite_mode:
            self._ensure_sqlite()

    # ─── Azure init ──────────────────────────────────────────────────────────

    def _init_azure(self):
        """Attempt to create an Azure TableServiceClient. Returns None on failure."""
        try:
            from azure.data.tables import TableServiceClient
            from azure.identity import DefaultAzureCredential
            import logging
            # Suppress noisy Azure identity credential logs
            for logger_name in ("azure.identity", "azure.identity._credentials",
                                "azure.core.pipeline.policies.http_logging_policy"):
                logging.getLogger(logger_name).setLevel(logging.CRITICAL)

            conn_str = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "")
            account_key = os.getenv("AZURE_STORAGE_ACCOUNT_KEY", "")
            account_name = _get_account_name()
            endpoint = _table_endpoint(account_name) if account_name else "Azure Storage"

            if conn_str:
                service = TableServiceClient.from_connection_string(conn_str)
            elif account_name and account_key:
                conn_str = f"DefaultEndpointsProtocol=https;AccountName={account_name};AccountKey={account_key};EndpointSuffix=core.windows.net"
                service = TableServiceClient.from_connection_string(conn_str)
            elif account_name:
                service = TableServiceClient(endpoint=endpoint, credential=DefaultAzureCredential())
            else:
                print("  [Tracker-WARN] AZURE_STORAGE_ACCOUNT_NAME / AZURE_BLOB_URL not set.")
                return None

            # Ensure the table exists (create if missing — idempotent)
            try:
                service.create_table_if_not_exists(TABLE_NAME)
            except Exception as e:
                if "AuthorizationPermissionMismatch" in str(e):
                    print("  [Tracker-WARN] Azure Table RBAC permission missing ('Storage Table Data Contributor').")
                    print("  [Tracker-INFO] Falling back to dual-write mode (SQLite + Azure Table when authorized).")
                else:
                    print(f"  [Tracker-WARN] Could not ensure table: {e}")

            print(f"  [Tracker] Connected to Azure Table '{TABLE_NAME}' at {endpoint}")
            return service.get_table_client(TABLE_NAME)

        except ImportError:
            print("  [Tracker-WARN] azure-data-tables not installed. Run: pip install azure-data-tables")
            return None
        except Exception as e:
            print(f"  [Tracker-WARN] Azure Table init failed: {type(e).__name__}: {e}")
            return None

    # ─── SQLite fallback init ─────────────────────────────────────────────────

    def _ensure_sqlite(self):
        """Ensure the local SQLite DB exists with the required schema."""
        try:
            from setup_email_tracker import init_db
            init_db()
        except Exception as e:
            print(f"  [Tracker-WARN] SQLite init_db failed: {e}")

    # ─── Public API ───────────────────────────────────────────────────────────

    def get_email(self, message_id: str) -> dict | None:
        """
        Fetch a single email entity by message_id.

        Returns:
            dict of entity properties, or None if not found.
        """
        if self._sqlite_mode:
            return self._sqlite_get(message_id)
        return self._azure_get(message_id)

    def upsert_email(self, message_id: str, **fields) -> None:
        """
        Insert a new email record OR update fields on an existing one.

        Equivalent to SQLite's INSERT OR IGNORE + UPDATE for changed fields.
        Existing field values are NOT overwritten unless explicitly passed.

        Args:
            message_id: Graph API message ID (RowKey).
            **fields:   Any combination of: subject, received_date, status,
                        source_file, error_log, so_number, block_code,
                        block_reason, stage, conversation_id, attachments.
        """
        if self._sqlite_mode:
            self._sqlite_upsert(message_id, **fields)
            return
        self._azure_upsert(message_id, **fields)

    def update_status(self, message_id: str, status: str,
                      extra_fields: dict | None = None) -> None:
        """
        Update only the status (and optionally other fields) for an email.

        Args:
            message_id:  Graph API message ID.
            status:      New status value (e.g. 'ROBONA_SENT', 'SO_BLOCKED').
            extra_fields: Optional additional fields to set at the same time.
        """
        fields = {"status": status, "updated_at": _now_utc()}
        if extra_fields:
            fields.update(extra_fields)
        if self._sqlite_mode:
            self._sqlite_upsert(message_id, **fields)
            return
        self._azure_upsert(message_id, **fields)

    def get_eligible_emails(self, statuses: list) -> list:
        """
        Return all emails whose status is one of the given values.

        Equivalent to:
          SELECT message_id, status, source_file, subject, conversation_id
          FROM processed_emails WHERE status IN (...)

        Returns:
            List of dicts with keys: message_id (= RowKey), status, source_file,
            subject, conversation_id (plus any other stored properties).
        """
        if self._sqlite_mode:
            return self._sqlite_query_statuses(statuses)
        return self._azure_query_statuses(statuses)

    def record_attachment(self, message_id: str, filename: str) -> None:
        """
        Record an attachment filename for a message.
        Appends to the 'attachments' property (comma-separated list).
        """
        existing = self.get_email(message_id)
        if existing:
            current = existing.get("attachments", "") or ""
            names   = [n.strip() for n in current.split(",") if n.strip()]
            if filename not in names:
                names.append(filename)
            self.upsert_email(message_id, attachments=",".join(names))
        else:
            self.upsert_email(message_id, attachments=filename)

    def get_email_by_filename(self, filename: str) -> dict | None:
        """
        Fetch an email record by matching the attachment filename or source_file.

        Returns:
            dict of email entity properties (including sender_email), or None.
        """
        if not filename:
            return None
        target = os.path.basename(filename).strip().lower()

        # Try Azure Table Storage if connected
        if not self._sqlite_mode and self._azure_client is not None:
            try:
                filter_str = f"PartitionKey eq '{PARTITION_KEY}'"
                entities = self._azure_client.query_entities(filter_str)
                for e in entities:
                    row = dict(e)
                    src = str(row.get("source_file", "") or "").strip().lower()
                    atts = [a.strip().lower() for a in str(row.get("attachments", "") or "").split(",") if a.strip()]
                    if target == src or target in atts or any(target in a for a in atts):
                        row.setdefault("message_id", row.get("RowKey", ""))
                        return row
            except Exception:
                pass

        # Fallback: query SQLite
        return self._sqlite_get_by_filename(target)

    def _sqlite_get_by_filename(self, target_fn: str) -> dict | None:
        try:
            conn = self._sqlite_conn()
            cursor = conn.cursor()
            for col in ("conversation_id", "attachments", "sender_email"):
                try:
                    cursor.execute(f"ALTER TABLE processed_emails ADD COLUMN {col} TEXT")
                except Exception:
                    pass
            row = cursor.execute(
                "SELECT * FROM processed_emails WHERE LOWER(source_file)=? OR LOWER(attachments) LIKE ?",
                (target_fn, f"%{target_fn}%")
            ).fetchone()
            conn.close()
            return dict(row) if row else None
        except Exception:
            return None

    def get_emails_by_po_number(self, po_number: str) -> list:
        """
        Return all tracker records for a given PO number.

        Used by the duplicate-SO guard: if any previous email for this PO number
        is in a terminal-success state (ROBONA_SENT, SO_BLOCKED, PUSHED_TO_CELONIS,
        PENDING_STAGE2, SKIP_DUPLICATE) the current submission is a duplicate.

        Stored in Azure Table Storage ONLY (no SQLite fallback for this check —
        set AZURE_TABLE_FALLBACK_SQLITE=false to enforce Azure-only mode).

        Returns:
            List of dicts with at least: message_id, status, po_number, source_file.
        """
        if not po_number:
            return []
        po_clean = str(po_number).strip()

        # ── SQLite mode (force_sqlite=True or Azure unavailable + FALLBACK_SQLITE=true) ──
        if self._sqlite_mode:
            return self._sqlite_get_by_po(po_clean.lower())

        # ── Azure Table: use OData server-side filter (no client-side full scan) ──────
        if self._azure_client is not None:
            try:
                # OData filter: case-insensitive PO match
                # Azure Table OData does not support LOWER(), so we store po_number
                # already lowercased at write time (see update_status calls with po_number).
                # We also try exact match with original casing as fallback.
                po_lower = po_clean.lower()
                filter_str = (
                    f"PartitionKey eq '{PARTITION_KEY}' and "
                    f"(po_number eq '{po_lower}' or po_number eq '{po_clean}')"
                )
                entities = self._azure_client.query_entities(filter_str)
                results = []
                for e in entities:
                    row = dict(e)
                    row.setdefault("message_id", row.get("RowKey", ""))
                    results.append(row)
                return results
            except Exception as _e:
                print(f"  [Tracker-WARN] get_emails_by_po_number Azure query failed: {_e}")
                if FALLBACK_SQLITE:
                    return self._sqlite_get_by_po(po_clean.lower())
                # AZURE_TABLE_FALLBACK_SQLITE=false: return empty (safe — no dups created)
                return []

        # Azure client is None and we are not in sqlite_mode → Azure init failed but
        # FALLBACK_SQLITE=false raised RuntimeError in __init__, so this is unreachable.
        return []

    def _sqlite_get_by_po(self, po_number_lower: str) -> list:
        """SQLite fallback for get_emails_by_po_number (only used when force_sqlite=True
        or AZURE_TABLE_FALLBACK_SQLITE=true and Azure is unreachable)."""
        try:
            conn = self._sqlite_conn()
            cursor = conn.cursor()
            # Ensure po_number column exists (migration)
            try:
                cursor.execute("ALTER TABLE processed_emails ADD COLUMN po_number TEXT")
                conn.commit()
            except Exception:
                pass
            rows = cursor.execute(
                "SELECT message_id, status, source_file, subject, po_number, so_number "
                "FROM processed_emails WHERE LOWER(po_number)=?",
                (po_number_lower,)
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def create_table_if_not_exists(self) -> None:
        """Explicit table creation — called by setup_email_tracker.py init."""

        if self._sqlite_mode:
            self._ensure_sqlite()
            return
        if self._azure_client is not None:
            print(f"  [Tracker] Azure Table '{TABLE_NAME}' already ensured during init.")

    # ─── Azure internals ──────────────────────────────────────────────────────

    def _build_entity(self, message_id: str, **fields) -> dict:
        entity = {
            "PartitionKey": PARTITION_KEY,
            "RowKey":       message_id,
        }
        entity.update({k: str(v) if v is not None else "" for k, v in fields.items()})
        return entity

    def _azure_get(self, message_id: str) -> dict | None:
        try:
            entity = self._azure_client.get_entity(
                partition_key=PARTITION_KEY,
                row_key=message_id,
            )
            result = dict(entity)
            result.setdefault("message_id", result.get("RowKey", message_id))
            return result
        except Exception:
            if FALLBACK_SQLITE:
                return self._sqlite_get(message_id)
            return None

    def _azure_upsert(self, message_id: str, **fields) -> None:
        from azure.data.tables import UpdateMode
        entity = self._build_entity(message_id, **fields)
        azure_ok = False
        try:
            self._azure_client.create_entity(entity=entity)
            azure_ok = True
        except Exception:
            try:
                self._azure_client.update_entity(
                    entity=entity,
                    mode=UpdateMode.MERGE,
                )
                azure_ok = True
            except Exception as e:
                # If Azure write fails (e.g. RBAC permission mismatch), fall back to SQLite
                if FALLBACK_SQLITE:
                    self._sqlite_upsert(message_id, **fields)

    def _azure_query_statuses(self, statuses: list) -> list:
        """Query Azure Table for all emails matching any of the given statuses.

        IMPORTANT: If Azure Table is connected and returns 0 results, that IS the
        truth (no eligible emails yet). We must NOT fall back to SQLite in this case —
        SQLite has stale PENDING_STAGE2 records that would cause duplicate Robona sends.
        Fallback to SQLite is ONLY done when Azure is unreachable (exception).
        """
        try:
            conditions = " or ".join(f"status eq '{s}'" for s in statuses)
            filter_str = f"PartitionKey eq '{PARTITION_KEY}' and ({conditions})"
            entities   = self._azure_client.query_entities(filter_str)
            results = []
            for e in entities:
                row = dict(e)
                row.setdefault("message_id", row.get("RowKey", ""))
                results.append(row)
            # Return Azure results directly — empty list from Azure is valid truth.
            # Do NOT fall back to SQLite when Azure is connected and responds.
            return results
        except Exception as e:
            # Only fall back to SQLite on connection/query failure
            if FALLBACK_SQLITE:
                print(f"  [Tracker-WARN] Azure query failed ({e}) — falling back to SQLite.")
                return self._sqlite_query_statuses(statuses)
            return []


    # ─── SQLite fallback internals ────────────────────────────────────────────

    def _sqlite_conn(self):
        conn = sqlite3.connect(SQLITE_PATH)
        conn.row_factory = sqlite3.Row
        return conn

    def _sqlite_get(self, message_id: str) -> dict | None:
        try:
            conn = self._sqlite_conn()
            row  = conn.execute(
                "SELECT * FROM processed_emails WHERE message_id=?", (message_id,)
            ).fetchone()
            conn.close()
            return dict(row) if row else None
        except Exception:
            return None

    def _sqlite_upsert(self, message_id: str, **fields) -> None:
        try:
            conn   = self._sqlite_conn()
            cursor = conn.cursor()
            # Safety migration for new columns (including po_number for dedup guard)
            for col in ("conversation_id", "attachments", "sender_email", "po_number"):
                try:
                    cursor.execute(f"ALTER TABLE processed_emails ADD COLUMN {col} TEXT")
                except Exception:
                    pass

            # INSERT OR IGNORE (preserves existing data)
            cols         = ["message_id"] + list(fields.keys())
            vals         = [message_id]   + list(fields.values())
            placeholders = ",".join("?" * len(cols))
            cursor.execute(
                f"INSERT OR IGNORE INTO processed_emails ({','.join(cols)}) "
                f"VALUES ({placeholders})",
                vals,
            )
            # UPDATE changed fields on existing row
            if fields:
                set_clause = ", ".join(f"{k}=?" for k in fields)
                cursor.execute(
                    f"UPDATE processed_emails SET {set_clause} WHERE message_id=?",
                    list(fields.values()) + [message_id],
                )
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"  [Tracker-WARN] SQLite upsert failed: {e}")

    def _sqlite_query_statuses(self, statuses: list) -> list:
        try:
            placeholders = ",".join("?" * len(statuses))
            conn = self._sqlite_conn()
            rows = conn.execute(
                f"SELECT message_id, status, source_file, subject, conversation_id, po_number, so_number "
                f"FROM processed_emails WHERE status IN ({placeholders})",
                statuses,
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as e:
            print(f"  [Tracker-WARN] SQLite query failed: {e}")
            return []


# ─── Module-level singleton ───────────────────────────────────────────────────

_default_tracker = None


def get_tracker() -> AzureEmailTracker:
    """Return the module-level singleton AzureEmailTracker (lazy init)."""
    global _default_tracker
    if _default_tracker is None:
        _default_tracker = AzureEmailTracker()
    return _default_tracker
