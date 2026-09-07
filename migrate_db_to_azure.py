"""
migrate_db_to_azure.py
======================
One-time migration script: copy all rows from local SQLite processed_emails.db
into Azure Table Storage.

Safe to re-run — uses upsert (MERGE mode) so existing Azure entities are not
duplicated or overwritten with stale data.

Usage
-----
  python migrate_db_to_azure.py                  # live migration
  python migrate_db_to_azure.py --dry-run        # preview only, no writes
  python migrate_db_to_azure.py --verify         # compare SQLite vs Azure row counts
"""

import os
import sys
import sqlite3
import argparse
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

SQLITE_PATH = Path(__file__).parent / "processed_emails.db"


def migrate(dry_run: bool = False):
    print("=" * 65)
    print("  SQLite -> Azure Table Storage Migration")
    print("=" * 65)

    # ── 1. Open SQLite ────────────────────────────────────────────────
    if not SQLITE_PATH.exists():
        print(f"  [ERROR] SQLite DB not found: {SQLITE_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM processed_emails").fetchall()
    conn.close()
    print(f"  [SQLite] Found {len(rows)} row(s) to migrate.")

    if not rows:
        print("  [INFO] Nothing to migrate. Done.")
        return

    # ── 2. Init Azure tracker ─────────────────────────────────────────
    from azure_email_tracker import AzureEmailTracker
    tracker = AzureEmailTracker()

    if tracker._sqlite_mode:
        print("  [ERROR] Azure Table Storage is not available. Cannot migrate.")
        print("  Check AZURE_BLOB_URL / AZURE_STORAGE_ACCOUNT_NAME and credentials.")
        sys.exit(1)

    print(f"  [Azure] Target table: processedemails")
    if dry_run:
        print("  [DRY-RUN] No writes will be made.\n")

    # ── 3. Migrate row by row ─────────────────────────────────────────
    migrated  = 0
    skipped   = 0
    errors    = 0

    for row in rows:
        d = dict(row)
        msg_id = d.pop("message_id", None)
        if not msg_id:
            skipped += 1
            continue

        # Sanitize None → "" for Azure Table (doesn't accept None values)
        fields = {k: (str(v) if v is not None else "") for k, v in d.items()}

        if dry_run:
            if migrated < 5 or migrated % 100 == 0:
                print(f"  [DRY-RUN] Would upsert: {msg_id[:24]}... status={fields.get('status','?')}")
            migrated += 1
            continue

        try:
            tracker.upsert_email(msg_id, **fields)
            migrated += 1
            if migrated % 25 == 0:
                print(f"  [Progress] Migrated {migrated}/{len(rows)} rows...")
        except Exception as e:
            print(f"  [ERROR] Failed to migrate {msg_id[:24]}...: {e}")
            errors += 1

    # ── 4. Summary ────────────────────────────────────────────────────
    print()
    print("=" * 65)
    print(f"  Migration {'(DRY-RUN) ' if dry_run else ''}complete.")
    print(f"  Migrated : {migrated}")
    print(f"  Skipped  : {skipped}")
    print(f"  Errors   : {errors}")
    print("=" * 65)
    if not dry_run and errors == 0:
        print("  [OK] All rows migrated successfully.")
        print("  Verify in Azure Portal -> Storage Account -> Tables -> processedemails")
    elif errors > 0:
        print(f"  [WARN] {errors} row(s) failed. Re-run to retry -- upsert is idempotent.")


def verify():
    """Compare SQLite row count vs Azure Table entity count."""
    print("\n[Verify] Comparing SQLite vs Azure Table row counts...")

    if not SQLITE_PATH.exists():
        print(f"  [ERROR] SQLite DB not found: {SQLITE_PATH}")
        return

    conn  = sqlite3.connect(SQLITE_PATH)
    count = conn.execute("SELECT COUNT(*) FROM processed_emails").fetchone()[0]
    conn.close()
    print(f"  SQLite rows : {count}")

    from azure_email_tracker import AzureEmailTracker
    tracker = AzureEmailTracker()
    if tracker._sqlite_mode:
        print("  [ERROR] Azure not available -- cannot verify.")
        return

    try:
        entities = list(tracker._azure_client.list_entities())
        print(f"  Azure rows  : {len(entities)}")
        if len(entities) >= count:
            print("  [OK] Azure has at least as many rows as SQLite -- migration looks complete.")
        else:
            diff = count - len(entities)
            print(f"  [WARN] Azure is missing ~{diff} row(s). Re-run migration.")
    except Exception as e:
        print(f"  [ERROR] Azure count failed: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Migrate processed_emails.db (SQLite) to Azure Table Storage"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview migration — print what would be written but don't write"
    )
    parser.add_argument(
        "--verify", action="store_true",
        help="Compare SQLite vs Azure row counts after migration"
    )
    args = parser.parse_args()

    if args.verify:
        verify()
    else:
        migrate(dry_run=args.dry_run)
