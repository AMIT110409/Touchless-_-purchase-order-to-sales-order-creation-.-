import json, sys
sys.stdout.reconfigure(encoding="utf-8")

with open("results_outlook_po_extracted_enriched.jsonl", encoding="utf-8") as f:
    for line in f:
        rec = json.loads(line)
        sf = rec.get("source_file", "")
        cn = str(rec.get("header_fields", {}).get("customer_name", "")).lower()
        if "molex" in sf.lower() or "molex" in cn:
            hf = rec.get("header_fields", {})
            print("=== MOLEX PO ===")
            print(f"  source_file      : {sf}")
            print(f"  customer_name    : {hf.get('customer_name')}")
            print(f"  customer_number  : {hf.get('customer_number')}")
            print(f"  customer_id      : {hf.get('customer_id')}")
            print(f"  sold_to_id       : {hf.get('sold_to_id')}")
            print(f"  ship_to_id       : {hf.get('ship_to_id')}")
            print(f"  sales_org        : {hf.get('sales_organization')}")
            print()
            for so in rec.get("sales_orders", []):
                print(f"  SO customer_number : {so.get('customer_number')}")
                print(f"  SO sold_to_id      : {so.get('sold_to_id')}")
                print(f"  SO sales_org       : {so.get('sales_organization')}")
                for item in so.get("items", []):
                    ext = item.get("extracted_material_number")
                    internal = item.get("internal_material_number")
                    cust_mat = item.get("customer_material_number")
                    desc = item.get("material_description", "")
                    print(f"  Item: extracted={ext}  internal={internal}  cust_mat={cust_mat}")
                    print(f"        desc={desc[:60]}")
