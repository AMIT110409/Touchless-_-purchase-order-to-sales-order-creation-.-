import pandas as pd
import sqlite3
import os
os.environ['DISABLE_MODEL_SOURCE_CHECK'] = 'True'

import argparse

def clean_column_names(df):
    """Normalize Celonis technical names to readable names."""
    new_cols = []
    for c in df.columns:
        if '}' in c:
            name = c.split('.')[-1].replace('}', '').strip()
        else:
            name = c.strip()
        new_cols.append(name.lower().replace(' ', '_'))
    df.columns = new_cols
    return df

def build_kb(vendors_csv='vendors.csv', materials_csv='materials.csv', db_path='knowledge_base.db'):
    print(f"Reading {vendors_csv}...")
    df_vendors = pd.read_csv(vendors_csv, dtype=str)
    
    print(f"Reading {materials_csv} (this may take a moment)...")
    df_materials = pd.read_csv(materials_csv, dtype=str)

    # Clean PQL headers
    df_vendors = clean_column_names(df_vendors)
    df_materials = clean_column_names(df_materials)

    print("\nProcessing Vendor Master Data...")
    # Map to: customer_number, name, sold_to, ship_to, ship_to_name
    df_v_final = df_vendors[['number', 'name', 'sold_to', 'ship_to', 'ship_to_name']].copy()
    df_v_final.rename(columns={'number': 'customer_number'}, inplace=True)
    df_v_final.drop_duplicates(inplace=True)

    print("Processing Material Master Data...")
    # Map to: customer_number, material_description, internal_material_number, customer_material_number, sales_org
    # materials.csv has: number, name, sold_to, ship_to, ship_to_name, material, customermaterialnumber, salesorganization
    df_m_final = df_materials[['number', 'material', 'customermaterialnumber', 'salesorganization']].copy()
    df_m_final.rename(columns={
        'number': 'customer_number',
        'material': 'internal_material_number',
        'customermaterialnumber': 'customer_material_number',
        'salesorganization': 'sales_org'
    }, inplace=True)
    df_m_final['material_description'] = ''  # Not provided in Celonis extract
    
    # Reorder
    df_m_final = df_m_final[['customer_number', 'material_description', 'internal_material_number', 'customer_material_number', 'sales_org']]
    df_m_final.drop_duplicates(inplace=True)

    # Rebuild SQLite DB
    print(f"\nBuilding {db_path}...")
    if os.path.exists(db_path):
        os.remove(db_path)

    conn = sqlite3.connect(db_path)
    df_v_final.to_sql('vendors', conn, index=False)
    df_m_final.to_sql('materials', conn, index=False)
    
    # Create indexes for fast lookup during extraction
    conn.execute("CREATE INDEX idx_vendor_name ON vendors(name)")
    conn.execute("CREATE INDEX idx_vendor_num ON vendors(customer_number)")
    conn.execute("CREATE INDEX idx_mat_cust ON materials(customer_number)")
    conn.execute("CREATE INDEX idx_mat_internal ON materials(internal_material_number)")
    
    conn.commit()
    conn.close()
    
    print(f"SUCCESS: {db_path} created with:")
    print(f"  - {len(df_v_final)} unique Vendor rows")
    print(f"  - {len(df_m_final)} unique Material mapping rows")


if __name__ == "__main__":
    parser = argparse.ArgumentParser("Build Knowledge Base from Celonis Extracts")
    parser.add_argument("--fetch", action="store_true", help="Fetch fresh data from Celonis before building")
    args = parser.parse_args()

    if args.fetch:
        print("Fetching latest data from Celonis cloud... (This replicates the Jupyter Notebook code)")
        from pycelonis import get_celonis
        import pycelonis.pql as pql
        from pycelonis.pql.saola_connector import AnalysisSaolaConnector
        
        url = 'https://envalior-sb.eu-1.celonis.cloud/'
        token = 'NTFkY2QyZTQtMDQ4OS00MTljLThhMGUtMWJkMGRlOTUxNTkzOk5KM0RITkh2eVJvZmt6Z0ZvU05tT0s2MHRwMnc5cGhtaGw1a1l3L0tkTFAx'
        c = get_celonis(url, token) 
        
        space = c.apps.get_spaces().find('OCPM')
        data_pool = c.data_integration.get_data_pool('663e7e2d-74f0-4cf1-a0b4-ca4faf5fee06')
        package = space.get_packages().find('Touchless Order Creation')
        data_model = data_pool.get_data_model('3f193f92-a398-4989-915c-cc100e67d421')
        analysis = package.get_analysis('61ec7315-0e7a-467a-8b89-116d78e1ca9e#!')

        sheets = analysis.get_content().draft.document.components
        tables = {}
        for sheet_n in sheets:
            sheet = sheets.find(sheet_n.name)
            for component in sheet.components:
                 query = sheet.components.find_by_id(component.id).get_query() 
                 tables[component.id] = pql.DataFrame.from_pql(query, saola_connector=AnalysisSaolaConnector(data_model, analysis))

        print("Downloading Vendors Master Data...")
        t1 = tables['4087badc-15cc-4e8a-a4eb-ff580e6669c8'].to_pandas()
        t1.to_csv("vendors.csv", index=False)
        
        print("Downloading Materials Master Data...")
        t2 = tables['a2e6a325-299e-40a1-8d5f-9f80c836c3c5'].to_pandas()
        t2.to_csv("materials.csv", index=False)

    # Build DB from CSVs
    build_kb()
