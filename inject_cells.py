"""
Updates the 'po-upload-push-celonis' cell in TouchlessOrderCreation.ipynb
with the correct pycelonis 2.14.1 API (data_pool.create_table).
"""
import json

NB_PATH = "TouchlessOrderCreation.ipynb"

NEW_SOURCE = [
    "# -- Push DataFrame to Celonis Data Pool ----------------------------------------\n",
    "#\n",
    "#  pycelonis 2.x correct API:\n",
    "#    data_pool.create_table(\n",
    "#        df,                    # pandas DataFrame\n",
    "#        table_name,            # target table name in the data pool\n",
    "#        drop_if_exists=True,   # drops & recreates if table already exists\n",
    "#        force=True,            # allow replace without explicit column_config\n",
    "#        chunk_size=1000,       # rows per upload batch\n",
    "#    )\n",
    "#\n",
    "#  Prerequisites (already run in earlier cells):\n",
    "#    c          = get_celonis(url, api_token, key_type)\n",
    "#    data_pool  = c.data_integration.get_data_pool('663e7e2d-74f0-4cf1-a0b4-ca4faf5fee06')\n",
    "#    data_model = data_pool.get_data_model('3f193f92-a398-4989-915c-cc100e67d421')\n",
    "# ------------------------------------------------------------------------------\n",
    "\n",
    "print(f'Pushing {len(df_po)} rows -> Celonis table \"{TABLE_NAME}\" ...')\n",
    "\n",
    "try:\n",
    "    pool_table = data_pool.create_table(\n",
    "        df_po,\n",
    "        TABLE_NAME,\n",
    "        drop_if_exists=True,   # drops existing table and recreates\n",
    "        force=True,            # allows replace without specifying column_config\n",
    "        chunk_size=1000,       # upload 1 000 rows per batch\n",
    "    )\n",
    "    print(f'SUCCESS: Table \"{TABLE_NAME}\" uploaded to Celonis Data Pool.')\n",
    "    print(f'Table object: {pool_table}')\n",
    "\n",
    "except Exception as e:\n",
    "    print(f'ERROR pushing table: {e}')\n",
    "    raise\n",
    "\n",
    "# -- Reload the Data Model so the new table is visible in Celonis analyses ----─\n",
    "print('Reloading Data Model ...')\n",
    "try:\n",
    "    data_model.reload()\n",
    "    print('Data Model reloaded successfully.')\n",
    "except Exception as e:\n",
    "    print(f'Data Model reload skipped / failed: {e}')\n"
]

with open(NB_PATH, "r", encoding="utf-8") as f:
    nb = json.load(f)

updated = False
for cell in nb["cells"]:
    if cell.get("id") == "po-upload-push-celonis":
        cell["source"] = NEW_SOURCE
        cell["outputs"] = []
        cell["execution_count"] = None
        updated = True
        print("Updated cell: po-upload-push-celonis")
        break

if not updated:
    print("ERROR: Cell 'po-upload-push-celonis' not found in notebook!")

with open(NB_PATH, "w", encoding="utf-8") as f:
    json.dump(nb, f, indent=1, ensure_ascii=False)

print(f"Notebook saved: {NB_PATH}")
