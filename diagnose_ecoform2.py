import json, pandas as pd

data = json.load(open('final_output_fixed.json', encoding='utf-8'))
df = pd.read_excel('EXPORT_20260216_100124.XLSX', dtype=str)
df.columns = [c.strip() for c in df.columns]

# Load material map as in enrich_results
mat_df = pd.read_excel('EXPORT_20260216_100124.XLSX', dtype=str)
mat_df.columns = [c.strip() for c in mat_df.columns]

# Find the Ecoform record
for r in data:
    h = r.get('header_fields') or {}
    src = r.get('source_file', '')
    if 'ecoform' in src.lower() or '4674421' in str(h.get('po_number', '')):
        print("FILE:", src)
        print("FULL HEADER:")
        for k, v in h.items():
            print(f"  {k}: {repr(v)}")
        
        print("\nLINE ITEMS:")
        for item in r.get('line_items', []):
            print(f"  {item}")
        
        cust_id = h.get('sold_to_id') or h.get('customer_number') or h.get('customer_id_or_name')
        print(f"\nResolved cust_id for material lookup: {repr(cust_id)}")
        
        # Check what materials are in Excel for this customer
        print("\n=== Excel - check for cust_id in material columns ===")
        for col in mat_df.columns:
            hits = mat_df[mat_df[col].astype(str).str.contains(str(cust_id or ''), na=False)] if cust_id else []
            if len(hits) > 0:
                print(f"  Found in col '{col}':", list(hits[col]))
        break
