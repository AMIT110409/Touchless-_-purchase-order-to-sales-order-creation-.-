"""
Quick script to check what's in the PO_EXTRACTION_RESULTS table in Celonis.
"""
import os
import sys
from dotenv import load_dotenv

load_dotenv()

# Celonis config (from push_to_celonis.py / celonis_cells_all.py)
CELONIS_URL = 'https://envalior-sb.eu-1.celonis.cloud/'
API_TOKEN = 'NTFkY2QyZTQtMDQ4OS00MTljLThhMGUtMWJkMGRlOTUxNTkzOk5KM0RITkh2eVJvZmt6Z0ZvU05tT0s2MHRwMnc5cGhtaGw1a1l3L0tkTFAx'
POOL_ID = '663e7e2d-74f0-4cf1-a0b4-ca4faf5fee06'
DATA_MODEL_ID = '3f193f92-a398-4989-915c-cc100e67d421'
TABLE_NAME = 'PO_EXTRACTION_RESULTS'

print("=" * 60)
print("  Celonis Table Check")
print("=" * 60)
print(f"  URL        : {CELONIS_URL}")
print(f"  Pool ID    : {POOL_ID}")
print(f"  Table Name : {TABLE_NAME}")
print()

try:
    from pycelonis import get_celonis
    print("[1] Connecting to Celonis...")
    c = get_celonis(CELONIS_URL, API_TOKEN)
    print("    Connected!")

    print("[2] Getting Data Pool...")
    data_pool = c.data_integration.get_data_pool(POOL_ID)
    print(f"    Data Pool: {data_pool}")

    print("[3] Listing all tables in Data Pool...")
    tables = data_pool.get_tables()
    print(f"    Found {len(tables)} table(s):")
    for t in tables:
        print(f"      - {t.name}  (id: {t.id[:20] if hasattr(t, 'id') else '?'})")

    print(f"\n[4] Looking for table: '{TABLE_NAME}'...")
    try:
        target = tables.find(TABLE_NAME)
        print(f"    FOUND: {target}")
        print(f"    Table Name : {target.name}")
        if hasattr(target, 'id'):
            print(f"    Table ID   : {target.id}")

        # Try to read data
        print(f"\n[5] Reading data from '{TABLE_NAME}'...")
        try:
            df = target.get_data_frame()
            print(f"    Rows    : {len(df)}")
            print(f"    Columns : {len(df.columns)}")
            print(f"    Column names: {list(df.columns)}")
            print(f"\n    First 5 rows:")
            print(df.head(5).to_string())
            print(f"\n    Last 5 rows:")
            print(df.tail(5).to_string())
        except Exception as e:
            print(f"    Could not read data frame: {e}")
            print("    Trying alternative method...")
            try:
                import pycelonis.pql as pql
                # Try PQL query
                data_model = data_pool.get_data_model(DATA_MODEL_ID)
                print(f"    Data Model: {data_model}")
                dm_tables = data_model.get_tables()
                print(f"    Data Model tables ({len(dm_tables)}):")
                for dmt in dm_tables:
                    print(f"      - {dmt.name}")
            except Exception as e2:
                print(f"    Alternative also failed: {e2}")

    except Exception as e:
        print(f"    TABLE NOT FOUND: {e}")
        print(f"    Available tables: {[t.name for t in tables]}")

except ImportError:
    print("ERROR: pycelonis is not installed.")
    print("Install with: pip install pycelonis")
    print("Or: pip install --extra-index-url=https://pypi.celonis.cloud/ pycelonis")
except Exception as e:
    print(f"ERROR: {e}")
