
# --- CELL 0 ---
pip install --upgrade pycelonis

# --- CELL 1 ---
# Install the latest version of PyCelonis
!pip install --extra-index-url=https://pypi.celonis.cloud/ pycelonis

# --- CELL 2 ---
from pycelonis import get_celonis
import pycelonis.pql as pql
from pycelonis.pql.saola_connector import AnalysisSaolaConnector

# --- CELL 3 ---
url = 'https://envalior-sb.eu-1.celonis.cloud/'

api_token= 'NTFkY2QyZTQtMDQ4OS00MTljLThhMGUtMWJkMGRlOTUxNTkzOk5KM0RITkh2eVJvZmt6Z0ZvU05tT0s2MHRwMnc5cGhtaGw1a1l3L0tkTFAx'
# key_type = 'APP_KEY'

c = get_celonis(url, api_token) 

# --- CELL 13 ---
import pandas as pd

# -- Configuration ------------------------------------------------------------─
EXCEL_FILE = 'final_output_v8.xlsx'   # Enriched PO results file
TABLE_NAME = 'PO_EXTRACTION_RESULTS'  # Target table name in Celonis Data Pool
# ------------------------------------------------------------------------------

# Read the Excel file
df_po = pd.read_excel(EXCEL_FILE, dtype=str, engine='openpyxl')

# Normalise column names: UPPER_SNAKE_CASE (Celonis standard)
df_po.columns = [
    col.strip().upper().replace(' ', '_').replace('/', '_')
    for col in df_po.columns
]

print(f'Loaded {len(df_po)} rows x {len(df_po.columns)} columns from "{EXCEL_FILE}"')
print('Columns:', list(df_po.columns))
df_po.head(5)


# --- CELL 14 ---
# -- Push DataFrame to Celonis Data Pool ----------------------------------------
#
#  pycelonis 2.x correct API:
#    data_pool.create_table(
#        df,                    # pandas DataFrame
#        table_name,            # target table name in the data pool
#        drop_if_exists=True,   # drops & recreates if table already exists
#        force=True,            # allow replace without explicit column_config
#        chunk_size=1000,       # rows per upload batch
#    )
#
#  Prerequisites (already run in earlier cells):
#    c          = get_celonis(url, api_token, key_type)
#    data_pool  = c.data_integration.get_data_pool('663e7e2d-74f0-4cf1-a0b4-ca4faf5fee06')
#    data_model = data_pool.get_data_model('3f193f92-a398-4989-915c-cc100e67d421')
# ------------------------------------------------------------------------------

print(f'Pushing {len(df_po)} rows -> Celonis table "{TABLE_NAME}" ...')

try:
    pool_table = data_pool.create_table(
        df_po,
        TABLE_NAME,
        drop_if_exists=True,   # drops existing table and recreates
        force=True,            # allows replace without specifying column_config
        chunk_size=1000,       # upload 1 000 rows per batch
    )
    print(f'SUCCESS: Table "{TABLE_NAME}" uploaded to Celonis Data Pool.')
    print(f'Table object: {pool_table}')

except Exception as e:
    print(f'ERROR pushing table: {e}')
    raise

# -- Reload the Data Model so the new table is visible in Celonis analyses ----─
print('Reloading Data Model ...')
try:
    data_model.reload()
    print('Data Model reloaded successfully.')
except Exception as e:
    print(f'Data Model reload skipped / failed: {e}')

