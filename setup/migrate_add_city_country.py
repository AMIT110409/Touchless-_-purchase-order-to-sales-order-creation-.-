"""
migrate_add_city_country.py
----------------------------
Adds 'city' and 'country' columns to the existing vendors table in knowledge_base.db.
Safe to run multiple times (uses ALTER TABLE IF NOT EXISTS pattern).

Run this ONCE after the Celonis Sheet 2 sync adds city/country data.

Usage:
    python migrate_add_city_country.py
    python migrate_add_city_country.py --db knowledge_base.db
"""

import sqlite3
import argparse
import os

def migrate(db_path: str = "knowledge_base.db"):
    if not os.path.exists(db_path):
        print(f"[ERROR] Database not found: {db_path}")
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Check existing columns
    cursor.execute("PRAGMA table_info(vendors)")
    existing_cols = {row[1] for row in cursor.fetchall()}
    print(f"Existing vendors columns: {sorted(existing_cols)}")

    added = []

    if 'city' not in existing_cols:
        cursor.execute("ALTER TABLE vendors ADD COLUMN city TEXT DEFAULT ''")
        added.append('city')
        print("  -> Added column: city")
    else:
        print("  -> Column 'city' already exists, skipping.")

    if 'country' not in existing_cols:
        cursor.execute("ALTER TABLE vendors ADD COLUMN country TEXT DEFAULT ''")
        added.append('country')
        print("  -> Added column: country")
    else:
        print("  -> Column 'country' already exists, skipping.")

    conn.commit()
    conn.close()

    if added:
        print(f"\n[OK] Migration complete. Added: {added}")
        print("NOTE: city/country values will be empty until you re-run setup_knowledge_base.py")
        print("      or run: python celonis_to_azure.py --sheet2-only")
    else:
        print("\n[OK] No migration needed.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="knowledge_base.db", help="Path to SQLite DB")
    args = parser.parse_args()
    migrate(args.db)
