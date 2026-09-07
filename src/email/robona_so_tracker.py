"""
robona_so_tracker.py
====================
Azure Table Storage + Local JSON fallback tracker for Robona attachment-request emails.

One row per SAP Sales Order number — the SINGLE source of truth for
"has Robona been notified about this SO?".

Storage hierarchy:
  1. Azure Table Storage — table "robonasentlog"
  2. Local File Fallback  — ".celonis_cache/robona_sent_log.json"

Guarantees:
  - If Azure connection fails or auth token expires, the local JSON file
    is used so duplicate emails are NEVER sent for the same SO.
"""

import os
import json
import logging
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

# ─── Configuration ────────────────────────────────────────────────────────────
TABLE_NAME       = os.getenv("AZURE_ROBONA_LOG_TABLE", "robonasentlog")
PARTITION_KEY    = "robona"
LOCAL_CACHE_DIR  = Path(__file__).parent / ".celonis_cache"
LOCAL_LOG_PATH   = LOCAL_CACHE_DIR / "robona_sent_log.json"


def _get_account_name() -> str:
    name = os.getenv("AZURE_STORAGE_ACCOUNT_NAME", "")
    if name:
        return name
    blob_url = os.getenv("AZURE_BLOB_URL", "")
    if "blob.core.windows.net" in blob_url:
        return blob_url.split("//")[-1].split(".")[0]
    return ""


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class RobonaSOTracker:
    """
    Tracker keyed by SAP Sales Order number (SO#).

    Backed by Azure Table Storage (primary) + local JSON file fallback.
    Prevents duplicate email notifications when Azure credential fails.
    """

    def __init__(self):
        self._client        = None
        self._connected     = False
        self._sent_dict: dict = {}  # { so_number: record_dict }

        # 1. Load local cache first
        self._load_local_cache()

        # 2. Try initializing Azure
        self._client = self._init_azure()
        if self._client:
            self._connected = True
            print(f"  [RobonaTracker] Connected to Azure Table '{TABLE_NAME}'")
            # Sync Azure Table -> local cache & dict
            self._sync_azure_to_local()
        else:
            print(f"  [RobonaTracker-WARN] Could not connect to Azure Table '{TABLE_NAME}'.")
            print(f"  [RobonaTracker-INFO] Using local file tracker: {LOCAL_LOG_PATH}")

    # ─── Local JSON Cache ─────────────────────────────────────────────────────

    def _load_local_cache(self):
        """Load local JSON cache if exists."""
        try:
            if LOCAL_LOG_PATH.exists():
                with open(LOCAL_LOG_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        self._sent_dict = data
        except Exception as e:
            print(f"  [RobonaTracker-WARN] Failed to load local cache ({e})")

    def _save_local_cache(self):
        """Save local dict to JSON cache file."""
        try:
            LOCAL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            with open(LOCAL_LOG_PATH, "w", encoding="utf-8") as f:
                json.dump(self._sent_dict, f, indent=2)
        except Exception as e:
            print(f"  [RobonaTracker-WARN] Failed to save local cache ({e})")

    def _sync_azure_to_local(self):
        """Query Azure Table and merge into local dict & file."""
        if not self._connected or self._client is None:
            return
        try:
            filter_str = f"PartitionKey eq '{PARTITION_KEY}'"
            count = 0
            for entity in self._client.query_entities(filter_str):
                so = str(entity.get("RowKey", "") or "").strip()
                if so:
                    rec = dict(entity)
                    self._sent_dict[so] = rec
                    count += 1
            if count > 0:
                self._save_local_cache()
        except Exception as exc:
            print(f"  [RobonaTracker-WARN] Sync from Azure failed: {exc}")

    # ─── Azure Init ───────────────────────────────────────────────────────────

    def _init_azure(self):
        """Attempt to create an Azure TableClient. Returns None on failure."""
        try:
            from azure.data.tables import TableServiceClient
            from azure.identity import DefaultAzureCredential

            logging.getLogger("azure").setLevel(logging.WARNING)
            for logger_name in ("azure.identity", "azure.identity._credentials",
                                "azure.core.pipeline.policies.http_logging_policy"):
                logging.getLogger(logger_name).setLevel(logging.CRITICAL)

            conn_str     = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "")
            account_key  = os.getenv("AZURE_STORAGE_ACCOUNT_KEY", "")
            account_name = _get_account_name()
            endpoint     = f"https://{account_name}.table.core.windows.net" if account_name else ""

            if conn_str:
                svc = TableServiceClient.from_connection_string(conn_str)
            elif account_name and account_key:
                c_str = f"DefaultEndpointsProtocol=https;AccountName={account_name};AccountKey={account_key};EndpointSuffix=core.windows.net"
                svc = TableServiceClient.from_connection_string(c_str)
            elif account_name:
                svc = TableServiceClient(endpoint=endpoint, credential=DefaultAzureCredential())
            else:
                return None

            try:
                svc.create_table_if_not_exists(TABLE_NAME)
                print(f"  [RobonaTracker] Ensured Azure Table '{TABLE_NAME}'.")
            except Exception:
                pass

            return svc.get_table_client(TABLE_NAME)

        except Exception:
            return None

    # ─── Public API ──────────────────────────────────────────────────────────

    def is_sent(self, so_number: str) -> bool:
        """
        Return True if Robona has already been notified for this SO number.
        Checks in-memory / local cache + Azure Table.
        """
        so = str(so_number or "").strip()
        if not so:
            return False

        # Fast check against local dict / cache
        if so in self._sent_dict:
            return True

        # Azure check if connected
        if self._connected and self._client is not None:
            try:
                self._client.get_entity(partition_key=PARTITION_KEY, row_key=so)
                return True
            except Exception:
                pass

        return False

    def mark_sent(
        self,
        so_number:   str,
        po_number:   str = "",
        message_id:  str = "",
        source_file: str = "",
        sent_by:     str = "pipeline",
    ) -> bool:
        """
        Record that Robona was successfully notified for this SO number.
        Writes to local JSON cache AND Azure Table.
        """
        so = str(so_number or "").strip()
        if not so:
            return False

        rec = {
            "PartitionKey": PARTITION_KEY,
            "RowKey":       so,
            "sent_at":      _now_utc(),
            "po_number":    str(po_number  or ""),
            "message_id":   str(message_id or ""),
            "source_file":  str(source_file or ""),
            "sent_by":      str(sent_by or "pipeline"),
        }

        # 1. Update in-memory dict and local JSON cache (always succeeds)
        self._sent_dict[so] = rec
        self._save_local_cache()

        # 2. Update Azure Table if connected
        if self._connected and self._client is not None:
            try:
                from azure.data.tables import UpdateMode
                try:
                    self._client.create_entity(entity=rec)
                except Exception:
                    self._client.update_entity(entity=rec, mode=UpdateMode.MERGE)
                return True
            except Exception as exc:
                print(f"  [RobonaTracker-WARN] Azure write failed for SO '{so}': {exc}")

        return True

    def get_all_sent_so_numbers(self) -> set:
        """
        Return a set of ALL SO numbers that have been Robona-notified.
        Combines local JSON cache and Azure Table queries.
        """
        sent = set(self._sent_dict.keys())
        if self._connected and self._client is not None:
            try:
                filter_str = f"PartitionKey eq '{PARTITION_KEY}'"
                for entity in self._client.query_entities(filter_str):
                    so = str(entity.get("RowKey", "") or "").strip()
                    if so:
                        sent.add(so)
                        self._sent_dict[so] = dict(entity)
                self._save_local_cache()
            except Exception as exc:
                print(f"  [RobonaTracker-WARN] Azure query failed: {exc}")
        return sent

    def list_all_sent(self) -> list:
        """Return all sent SO records as a list of dicts."""
        return list(self._sent_dict.values())

    def get_sent_record(self, so_number: str) -> dict | None:
        """Return the record for a sent SO, or None if not found."""
        so = str(so_number or "").strip()
        return self._sent_dict.get(so)
