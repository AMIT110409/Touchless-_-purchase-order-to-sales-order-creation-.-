"""
setup_email_tracker.py
======================
Initialises the email tracking store.

Production (cloud-native): Azure Table Storage — 'processedemails' table
Local dev fallback        : SQLite processed_emails.db

Previously this file created and migrated the local SQLite DB schema.
Now it delegates to AzureEmailTracker, which handles both Azure and the
SQLite fallback transparently.

Run once to ensure the store is ready:
    python setup_email_tracker.py

Status Reference (Two-Stage Exception Handling)
------------------------------------------------
PENDING            : Email received, pipeline has not yet processed it
MAPPED_SUCCESS     : Extraction + material mapping succeeded (intermediate)
MAPPING_FAILED     : Material mapping failed → Stage 1 CSR exception sent
EXTRACTION_FAILED  : PDF/OCR extraction failed → Stage 1 CSR exception sent
EXCEPTION_ROUTED   : Stage 1 exception email sent, email → Exception POs
SKIP               : System-generated email (Robona/exception), ignored
PUSHED_TO_CELONIS  : Data sent to Celonis, email → Processed POs, awaiting Stage 2
SO_CREATED         : (reserved) Celonis confirmed SO created (use ROBONA_SENT)
SO_BLOCKED         : Stage 2: SO created but has an order block → CSR notified
SO_CREATION_FAILED : Stage 2: Action Flow failed to create SO → CSR notified + Exception POs
ROBONA_SENT        : Stage 2 complete: SO created successfully, Robona notified ✅
"""

import os
os.environ['DISABLE_MODEL_SOURCE_CHECK'] = 'True'

from pathlib import Path


def init_db():
    """
    Initialise the email tracking store.

    - Production: ensures the Azure Table 'processedemails' exists.
    - Local dev:  falls back to local SQLite processed_emails.db if Azure
                  is not available (AZURE_TABLE_FALLBACK_SQLITE=true, the default).
    """
    from azure_email_tracker import AzureEmailTracker
    tracker = AzureEmailTracker()
    tracker.create_table_if_not_exists()
    mode = "SQLite (fallback)" if tracker._sqlite_mode else "Azure Table Storage"
    print(f"Email tracker initialised — backend: {mode}")
    return tracker


# ─── Legacy SQLite migration helper ──────────────────────────────────────────
def _add_column_if_missing(cursor, table: str, column: str, col_type: str):
    """
    Legacy helper kept for backward compatibility with any script that calls it.
    No-op when using Azure Table Storage (schemaless — no migrations needed).
    """
    try:
        cursor.execute(f"PRAGMA table_info({table})")
        existing_cols = [row[1] for row in cursor.fetchall()]
        if column not in existing_cols:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            print(f"  [MIGRATION] Added column '{column}' to '{table}'")
    except Exception:
        pass  # Azure Table mode — no SQLite cursor available, skip silently


if __name__ == "__main__":
    init_db()
