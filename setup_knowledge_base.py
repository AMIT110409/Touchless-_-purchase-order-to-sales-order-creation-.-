import sqlite3
import pandas as pd
from pycelonis import get_celonis
import pycelonis.pql as pql
from pycelonis.pql.saola_connector import AnalysisSaolaConnector
import warnings
import os
os.environ['DISABLE_MODEL_SOURCE_CHECK'] = 'True'

from sqlalchemy import create_engine, text

# Suppress deprecation warnings
warnings.filterwarnings("ignore")

# ------------------------------------------------
# CONFIG (From TouchlessOrderCreation.ipynb)
# ------------------------------------------------
CELONIS_URL = 'https://envalior-sb.eu-1.celonis.cloud/'
API_TOKEN = 'NjUwMmJhOTEtMjUxOS00YTIyLThkNWUtNmZhYmRkOWE0MDIzOk5RajR3MW1vVWcxVjVqeHBGRlVIcGhMSFJSV3RRN2ZzVU9kOGd1M2pkYXFI'
KEY_TYPE = 'APP_KEY'

# IDs
DATA_POOL_ID = '663e7e2d-74f0-4cf1-a0b4-ca4faf5fee06'
DATA_MODEL_ID = '3f193f92-a398-4989-915c-cc100e67d421'
PACKAGE_NAME = 'Touchless Order Creation'
ANALYSIS_ID = '61ec7315-0e7a-467a-8b89-116d78e1ca9e#!'

# Component IDs for Tables
TABLE_VENDORS_ID = '4087badc-15cc-4e8a-a4eb-ff580e6669c8'
TABLE_MATERIALS_ID = 'a2e6a325-299e-40a1-8d5f-9f80c836c3c5'

DB_NAME = "knowledge_base.db"

def fetch_celonis_data():
    print("Connecting to Celonis...")
    try:
        c = get_celonis(CELONIS_URL, API_TOKEN, KEY_TYPE, permissions=False)
        
        # We need the data model and analysis objects to create the connector
        data_pool = c.data_integration.get_data_pool(DATA_POOL_ID)
        data_model = data_pool.get_data_model(DATA_MODEL_ID)
        
        space = c.apps.get_spaces().find('OCPM')
        package = space.get_packages().find(PACKAGE_NAME)
        analysis = package.get_analysis(ANALYSIS_ID)
        
        content = analysis.get_content()
        sheets = content.draft.document.components
        
        connector = AnalysisSaolaConnector(data_model, analysis)
        
        def get_df_from_id(comp_id, name):
            print(f"Fetching {name} data (ID: {comp_id})...")
            found_comp = None
            for sheet in sheets:
                 comp = sheet.components.find_by_id(comp_id)
                 if comp:
                     found_comp = comp
                     break
            
            if not found_comp:
                print(f"Error: Component {comp_id} not found in analysis.")
                return None

            query = found_comp.get_query()
            df = pql.DataFrame.from_pql(query, saola_connector=connector).to_pandas()
            print(f"  -> Fetched {len(df)} rows.")
            return df

        df_vendors = get_df_from_id(TABLE_VENDORS_ID, "Vendors")
        df_materials = get_df_from_id(TABLE_MATERIALS_ID, "Materials")
        
        return df_vendors, df_materials

    except Exception as e:
        print(f"Error connecting/fetching from Celonis: {e}")
        return None, None

def setup_sqlite(df_vendors, df_materials):
    print(f"Setting up database...")
    
    # Check for Azure Postgres Connection String
    db_conn_str = os.getenv("DB_CONNECTION_STRING")
    
    if db_conn_str:
        print("Using PostgreSQL (from Env Var)...")
        engine = create_engine(db_conn_str)
        is_sqlite = False
    else:
        print(f"Using local SQLite: {DB_NAME}...")
        engine = create_engine(f"sqlite:///{DB_NAME}")
        is_sqlite = True

    # 1. Vendors Table
    print("Creating vendors table...")
    with engine.connect() as conn:
        if is_sqlite:
            conn.execute(text("DROP TABLE IF EXISTS vendors"))
            conn.execute(text("""
                CREATE TABLE vendors (
                    customer_number TEXT,
                    name TEXT,
                    sold_to TEXT,
                    ship_to TEXT,
                    ship_to_name TEXT,
                    city TEXT,
                    country TEXT
                )
            """))
        else:
             # Postgres syntax
             conn.execute(text("DROP TABLE IF EXISTS vendors CASCADE"))
             conn.execute(text("""
                CREATE TABLE IF NOT EXISTS vendors (
                    customer_number TEXT,
                    name TEXT,
                    sold_to TEXT,
                    ship_to TEXT,
                    ship_to_name TEXT,
                    city TEXT,
                    country TEXT
                )
             """))
             # Clear data if exists to reload
             conn.execute(text("TRUNCATE TABLE vendors"))
             conn.commit()
    
    if df_vendors is not None:
        v_cols = df_vendors.columns
        print(f"Vendor Columns: {v_cols}")
        col_map = {}
        for c in v_cols:
            if "Customer.Number" in c: col_map[c] = "customer_number"
            elif "Name" in c and "Ship" not in c: col_map[c] = "name"
            elif "Sold to" in c: col_map[c] = "sold_to"
            elif "Ship to" in c and "Name" not in c and "name" not in c: col_map[c] = "ship_to"
            elif "Ship to Name" in c or "Ship to name" in c: col_map[c] = "ship_to_name"
            elif "City" in c or "city" in c: col_map[c] = "city"
            elif "Country" in c or "country" in c: col_map[c] = "country"
            
        df_v_clean = df_vendors.rename(columns=col_map)
        expected_cols = ["customer_number", "name", "sold_to", "ship_to", "ship_to_name", "city", "country"]
        for c in expected_cols:
            if c not in df_v_clean.columns: df_v_clean[c] = None 
        
        # Write to DB
        df_v_clean[expected_cols].to_sql("vendors", engine, if_exists="append", index=False)
        print("  -> Vendors table populated.")

    # 2. Materials Table
    print("Creating materials table...")
    with engine.connect() as conn:
        if is_sqlite:
            conn.execute(text("DROP TABLE IF EXISTS materials"))
            conn.execute(text("""
                CREATE TABLE materials (
                    customer_number TEXT,
                    internal_material_number TEXT,
                    customer_material_number TEXT,
                    sales_org TEXT
                )
            """))
        else:
             conn.execute(text("DROP TABLE IF EXISTS materials CASCADE"))
             conn.execute(text("""
                CREATE TABLE IF NOT EXISTS materials (
                    customer_number TEXT,
                    internal_material_number TEXT,
                    customer_material_number TEXT,
                    sales_org TEXT
                )
             """))
             # Clear data if exists to reload
             conn.execute(text("TRUNCATE TABLE materials"))
             conn.commit()
    
    if df_materials is not None:
        m_cols = df_materials.columns
        print(f"Material Columns: {m_cols}")
        col_map = {}
        for c in m_cols:
            if "CustomerO" in c: col_map[c] = "customer_number"
            elif "Material" in c and "Customer" not in c and "Number" not in c: col_map[c] = "internal_material_number"
            elif "Customer Material Number" in c: col_map[c] = "customer_material_number"
            elif "Sales Organization" in c: col_map[c] = "sales_org"
            
        df_m_clean = df_materials.rename(columns=col_map)
        expected_cols = ["customer_number", "internal_material_number", "customer_material_number", "sales_org"]
        for c in expected_cols:
            if c not in df_m_clean.columns: df_m_clean[c] = None
        
        df_m_clean[expected_cols].to_sql("materials", engine, if_exists="append", index=False)
        print("  -> Materials table populated.")

    print("Database setup complete.")

if __name__ == "__main__":
    v, m = fetch_celonis_data()
    if v is not None and m is not None:
        setup_sqlite(v, m)
    else:
        print("Skipping DB generation due to fetch error.")
