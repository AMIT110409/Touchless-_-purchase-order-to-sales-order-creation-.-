"""
Diagnostic: Query all rows in Celonis table 'PO_EXTRACTION_RESULTS' using PQL to inspect RUN_TIMESTAMP values.
"""
import sys, os
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
os.environ['DISABLE_MODEL_SOURCE_CHECK'] = 'True'
from dotenv import load_dotenv
load_dotenv()

from pycelonis import get_celonis
from pycelonis.pql.saola_connector import AnalysisSaolaConnector
import pycelonis.pql as pql
import pandas as pd

url        = 'https://envalior-sb.eu-1.celonis.cloud/'
api_token  = 'NTFkY2QyZTQtMDQ4OS00MTljLThhMGUtMWJkMGRlOTUxNTkzOk5KM0RITkh2eVJvZmt6Z0ZvU05tT0s2MHRwMnc5cGhtaGw1a1l3L0tkTFAx'
pool_id    = '663e7e2d-74f0-4cf1-a0b4-ca4faf5fee06'

c = get_celonis(url, api_token)
data_pool = c.data_integration.get_data_pool(pool_id)
dm = data_pool.get_data_model('3f193f92-a398-4989-915c-cc100e67d421')

print("=" * 70)
print("  Celonis Data Integration Audit — Table List")
print("=" * 70)
tables = data_pool.get_tables()
for t in tables:
    print(f"Table: {t.name:<35} | Data Source: {t.data_source_name}")

print("\n" + "=" * 70)
print("  Querying PO_EXTRACTION_RESULTS via PyCelonis PQL...")
print("=" * 70)

# Connect via AnalysisSaolaConnector
conn = AnalysisSaolaConnector(dm, None)

q = pql.PQL()
q.add(pql.PQLColumn(name='PO_NUMBER', query='"o_custom_PoExtractionResults"."PoNumber"'))
q.add(pql.PQLColumn(name='CUSTOMER_NAME', query='"o_custom_PoExtractionResults"."CustomerName"'))
q.add(pql.PQLColumn(name='SOURCE_FILE', query='"o_custom_PoExtractionResults"."SourceFile"'))

df = pql.DataFrame.from_pql(q, saola_connector=conn).to_pandas()
print(f"\n[OK] Total rows in Celonis Touchless View: {len(df)}")
print(df.tail(20).to_string())

mask_pos = df['PO_NUMBER'].astype(str).str.contains('4503206365|CSG0152081|13227', case=False, na=False)
print("\n--- Search for 4503206365 (TORAY) and CSG0152081 (INABATA) ---")
print(df[mask_pos].to_string())
