"""
audit_logger.py
===============
Structured per-PO audit trail for the Touchless PO-to-SAP pipeline.

STORAGE: Azure Blob ONLY  (celonis-tables/audit_logs/audit_YYYYMMDD.jsonl)
         No local writes. If Azure is unavailable the record is kept in memory
         and the next flush will retry. A warning is printed but the pipeline
         is never blocked.

Writes a JSONL record for every PO processed (success OR escalation) containing:
  - timestamp, po_number, source_email_id
  - all extracted header fields
  - customer matching tier + score used
  - pre-creation validation checklist results (PASS/FAIL per field)
  - final action: SO_CREATED / ESCALATED / EXTRACTION_FAILED / MAPPING_FAILED
  - sap_order_number (if created) / escalation_reason (if escalated)

Usage:
    from audit_logger import get_audit_logger
    logger = get_audit_logger()
    logger.log(po_number="PO-1234", action="SO_CREATED", matched_customer_id="4020010504")
    logger.flush()   # call once at end of pipeline run
"""

import json
import os
from datetime import datetime
from typing import Optional, List, Dict, Any


# ── Azure Blob config ─────────────────────────────────────────────────────────
BLOB_CONTAINER = "celonis-tables"
BLOB_LOG_DIR   = "audit_logs"


def _today_str() -> str:
    return datetime.utcnow().strftime("%Y%m%d")


def _now_str() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _get_blob_client(blob_name: str):
    """Return an Azure BlobClient for the audit log blob. Raises on failure."""
    from azure.storage.blob import BlobServiceClient
    blob_url = os.getenv("AZURE_BLOB_URL", "")
    if not blob_url:
        raise ValueError("AZURE_BLOB_URL env var is not set — cannot write audit log to Azure")
    account_key = os.getenv("AZURE_STORAGE_ACCOUNT_KEY")
    if account_key:
        svc = BlobServiceClient(account_url=blob_url, credential=account_key)
    else:
        from azure.identity import DefaultAzureCredential
        svc = BlobServiceClient(account_url=blob_url, credential=DefaultAzureCredential())
    return svc.get_blob_client(container=BLOB_CONTAINER, blob=blob_name)


class AuditLogger:
    """
    Accumulates audit records in memory and flushes to Azure Blob Storage.
    Azure is the ONLY destination — no local file writes.

    If Azure is unavailable at flush time, records are kept in the in-memory
    buffer and a clear warning is printed. The next flush will retry.
    """

    def __init__(self):
        self._records: List[Dict[str, Any]] = []

    # ── Public API ────────────────────────────────────────────────────────────

    def log(
        self,
        *,
        po_number: str,
        email_id: str = "",
        source_file: str = "",
        # Extraction
        extracted_fields: Optional[dict] = None,
        extraction_complete: bool = True,
        extraction_missing_fields: Optional[list] = None,
        # Customer matching
        customer_name_extracted: str = "",
        matching_method: str = "",
        matching_score: float = 0.0,
        matched_customer_id: str = "",
        matched_customer_name: str = "",
        candidate_customer_ids: Optional[list] = None,
        # Celonis
        celonis_records_considered: Optional[list] = None,
        celonis_record_selected: str = "",
        # Pre-creation validation
        validation_checks: Optional[dict] = None,
        # Final outcome
        action: str = "",
        escalation_reason: str = "",
        escalation_candidates: Optional[list] = None,
        sap_order_number: str = "",
        extra: Optional[dict] = None,
    ):
        """Append one audit record (kept in memory until flush())."""
        checks = validation_checks or {}
        record = {
            "timestamp":                  _now_str(),
            "run_date":                   _today_str(),
            "po_number":                  po_number or "",
            "email_id":                   email_id or "",
            "source_file":                source_file or "",
            "extraction_complete":        extraction_complete,
            "extraction_missing_fields":  extraction_missing_fields or [],
            "extracted_fields":           extracted_fields or {},
            "customer_name_extracted":    customer_name_extracted or "",
            "matching_method":            matching_method or "",
            "matching_score":             round(float(matching_score or 0), 3),
            "matched_customer_id":        matched_customer_id or "",
            "matched_customer_name":      matched_customer_name or "",
            "candidate_customer_ids":     candidate_customer_ids or [],
            "celonis_records_considered": celonis_records_considered or [],
            "celonis_record_selected":    celonis_record_selected or "",
            "validation_checks":          checks,
            "validation_passed":          all(v == "PASS" for v in checks.values() if v != "SKIP"),
            "action":                     action or "",
            "escalation_reason":          escalation_reason or "",
            "escalation_candidates":      escalation_candidates or [],
            "sap_order_number":           sap_order_number or "",
        }
        if extra:
            record.update(extra)
        self._records.append(record)

        icon = "[OK]" if action == "SO_CREATED" else ("[ESC]" if action == "ESCALATED" else "[ERR]")
        print(
            f"  [Audit] {icon} PO={po_number} | action={action}"
            f" | cust={matched_customer_id} | reason={escalation_reason or '-'}"
        )

    def flush(self):
        """
        Write all buffered records to Azure Blob Storage (ONLY).
        Uses append-style: downloads existing blob content, concatenates, re-uploads.
        Records are kept in buffer if Azure write fails — next flush will retry.
        """
        if not self._records:
            return

        filename = f"audit_{_today_str()}.jsonl"
        blob_name = f"{BLOB_LOG_DIR}/{filename}"
        lines = "\n".join(json.dumps(r, ensure_ascii=False) for r in self._records) + "\n"

        try:
            blob_client = _get_blob_client(blob_name)

            # Ensure container exists
            svc = blob_client._client  # type: ignore[attr-defined]
            try:
                blob_client.get_blob_properties()
                existing = blob_client.download_blob().readall().decode("utf-8")
            except Exception:
                existing = ""  # blob does not exist yet — start fresh

            blob_client.upload_blob(
                (existing + lines).encode("utf-8"),
                overwrite=True,
            )
            print(
                f"  [Audit] Flushed {len(self._records)} record(s) -> "
                f"Azure Blob: {BLOB_CONTAINER}/{blob_name}"
            )
            self._records.clear()  # clear ONLY on success

        except Exception as az_err:
            print(
                f"  [Audit-WARN] Azure Blob write FAILED — {len(self._records)} record(s) "
                f"kept in memory for next flush. Error: {az_err}"
            )
            # Do NOT clear records — they will be retried on next flush()

    def get_records(self) -> List[Dict[str, Any]]:
        """Return a snapshot of buffered records (for testing / reporting)."""
        return list(self._records)

    def record_count(self) -> int:
        """Number of records currently buffered (not yet flushed to Azure)."""
        return len(self._records)


# ── Module-level singleton ────────────────────────────────────────────────────
_default_logger: Optional[AuditLogger] = None


def get_audit_logger() -> AuditLogger:
    """Return (or create) the shared AuditLogger for this process."""
    global _default_logger
    if _default_logger is None:
        _default_logger = AuditLogger()
    return _default_logger


def reset_audit_logger() -> None:
    """Reset the singleton (useful for unit tests)."""
    global _default_logger
    _default_logger = None
