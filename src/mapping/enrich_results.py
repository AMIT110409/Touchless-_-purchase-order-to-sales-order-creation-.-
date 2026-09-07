"""
Enrich Extraction Results with Sold To & Ship To
-------------------------------------------------
Takes results_paddle.jsonl and adds Sold To / Ship To IDs from the Knowledge Base.
Outputs an enriched JSON file and optionally an Excel file.

Usage: python enrich_results.py --input results_paddle.jsonl --output enriched_results.json
"""

import json
import os
os.environ['DISABLE_MODEL_SOURCE_CHECK'] = 'True'

import argparse
from sqlalchemy import create_engine, text
import pandas as pd
import re

# DB Config
DB_CONNECTION_STRING = os.getenv("DB_CONNECTION_STRING")
DB_PATH = os.getenv("DB_PATH", "knowledge_base.db")

if DB_CONNECTION_STRING:
    db_engine = create_engine(DB_CONNECTION_STRING)
else:
    db_engine = create_engine(f"sqlite:///{DB_PATH}")

from thefuzz import process, fuzz

# Cache for vendor names (loaded once)
ALL_VENDOR_NAMES = []
VENDOR_NAME_MAP = {} # name -> list of full records

def load_all_vendors():
    """
    Loads all vendor names from DB into memory for fuzzy matching.
    """
    global ALL_VENDOR_NAMES, VENDOR_NAME_MAP
    
    if ALL_VENDOR_NAMES:
        return

    print("Loading vendor master data for fuzzy matching...")
    try:
        with db_engine.connect() as conn:
            query = text("SELECT customer_number, name, sold_to, ship_to, ship_to_name FROM vendors")
            rows = conn.execute(query).fetchall()
            
            for r in rows:
                v_name = r[1]
                if not v_name: continue
                
                # Normalize slightly
                v_name_clean = v_name.strip()
                
                if v_name_clean not in VENDOR_NAME_MAP:
                    VENDOR_NAME_MAP[v_name_clean] = []
                    ALL_VENDOR_NAMES.append(v_name_clean)
                
                VENDOR_NAME_MAP[v_name_clean].append({
                    "customer_number": r[0],
                    "name": r[1],
                    "sold_to": r[2],
                    "ship_to": r[3],
                    "ship_to_name": r[4]
                })
            
            print(f"  -> Loaded {len(ALL_VENDOR_NAMES)} unique vendor names.")
            
    except Exception as e:
        print(f"DB Error loading vendors: {e}")

def clean_vendor_name(name):
    """
    Remove common corporate suffixes/noise to improve fuzzy matching.
    """
    if not name: return ""
    name_lower = name.lower()
    noise = ["gmbh", "co.", "co", "ltd", "ltd.", "limited", "inc", "inc.", "corp", "corporation", "group", "ab", "s.r.l.", "srl", "sas", "s.a.s.", "ag", "plc", "llc", "lp"]
    
    # Simple replacement for noise words. 
    # Logic: replace " word " with " " or ending " word" with ""
    for n in noise:
        name_lower = name_lower.replace(f" {n} ", " ")
        if name_lower.endswith(f" {n}"):
            name_lower = name_lower[:-len(n)-1]
        if name_lower.startswith(f"{n} "):
            name_lower = name_lower[len(n)+1:]
            
    return name_lower.strip()

def get_vendor_master_data(vendor_name):
    """
    Get master data for a given vendor name using FUZZY MATCHING.
    Returns list of dicts.
    """
    if not vendor_name:
        return []
        
    load_all_vendors()
    
    # 1. Exact Match (Fast)
    if vendor_name in VENDOR_NAME_MAP:
        return VENDOR_NAME_MAP[vendor_name]
    
    # Prep for fuzzy
    vendor_name_clean = clean_vendor_name(vendor_name)
    if not vendor_name_clean:
         vendor_name_clean = vendor_name # Fallback if cleaning removed everything

    # 2. Fuzzy Match
    # Strategy:
    # A) Try token_sort_ratio (good for full name matches with reordered words)
    # B) Try partial_ratio on CLEANED name (good for "Stebro" -> "Stebro Plast AB")
    
    matches = []
    
    # A) Token Sort Ratio
    # Strict threshold to avoid false positives on short names (e.g. Stebro -> STEGO)
    match_tuple = process.extractOne(vendor_name, ALL_VENDOR_NAMES, scorer=fuzz.token_sort_ratio)
    if match_tuple and match_tuple[1] >= 85:
        best_match_name = match_tuple[0]
        # Verify it's not a short-name false positive if the input is short (len < 5)
        if len(vendor_name) < 5 and match_tuple[1] < 100:
             pass 
        else:
             print(f"    (Fuzzy Match [Sort]: '{vendor_name}' -> '{best_match_name}' [Score: {match_tuple[1]}])")
             matches.extend(VENDOR_NAME_MAP[best_match_name])

    # B) Partial Ratio with Cleaning/Cleanup
    # Use extract to get TOP 5 matches, then filter by score
    # This ensures if we have "Ecoform" and "Ecoform Inc", we get both
    raw_matches = process.extract(vendor_name_clean, ALL_VENDOR_NAMES, scorer=fuzz.partial_ratio, limit=10)
    
    for match_name, score in raw_matches:
        # Filter dangerous short matches (e.g. "KU" matching "VerpacKUng...")
        if len(match_name) < 5:
            # Only accept short match if it's somewhat similar in full text
            if fuzz.token_sort_ratio(vendor_name, match_name) < 60:
                continue
                
        if score >= 90:
            print(f"    (Fuzzy Match [Clean Partial]: '{vendor_name}' -> '{match_name}' [Score: {score}])")
            # Add to matches if not already there
            if match_name in VENDOR_NAME_MAP:
                for m in VENDOR_NAME_MAP[match_name]:
                    if m not in matches:
                        matches.append(m)
    
    return matches


def normalize_quantity_european(val):
    """
    Normalizes quantity string, handling European decimal formats (comma as decimal).
    e.g. '1.000,00' -> 1000.0
         '24,000'   -> 24.0 (if purely comma decimal assumption)
         '1,5'      -> 1.5
    """
    if val is None: return None
    
    # If already number, return extraction
    if isinstance(val, (int, float)):
        return val

    s = str(val).strip()
    if not s: return None
    
    # 1. Clean string: keep digits, ., , -
    cleaned = "".join(c for c in s if c.isdigit() or c in ".,-")
    
    if not cleaned: return None
    
    # 2. Heuristic for Decimal Separator
    # If ',' and '.' both present:
    #   '1.000,00' -> European (last separator is comma) -> remove dots, replace comma with dot
    #   '1,000.00' -> US (last separator is dot) -> remove commas
    
    if ',' in cleaned and '.' in cleaned:
        last_comma = cleaned.rfind(',')
        last_dot = cleaned.rfind('.')
        
        if last_comma > last_dot: # European
            cleaned = cleaned.replace('.', '').replace(',', '.')
        else: # US
            cleaned = cleaned.replace(',', '')
            
    elif ',' in cleaned:
        # Only comma. Ambiguous: "10,5" (10.5) vs "1,000" (1000).
        # Stebro context: "instead of . (dot) they use ,(comma)"
        # So we assume Comma = Decimal.
        # "24,000" -> 24.000 -> 24
        cleaned = cleaned.replace(',', '.')
        
    # Else (only dots or no separators) -> standard float parsing (Python handles 1000.00)
    
    try:
        f = float(cleaned)
        # Return int if integer
        if f.is_integer():
            result = int(f)
        else:
            result = f
            
        # DEBUG STEBRO QUANTITY
        if "3300" in str(val) or "6600" in str(val) or "4125" in str(val):
             print(f"    [DEBUG QTY] Input: {val!r} -> Cleaned: {cleaned!r} -> Result: {result!r}")
             
        return result
    except:
        return val # fallback to original if parsing fails

def fuzzy_match_ship_to(extracted_address, ship_to_options):
    """
    Matches extracted Ship To address against a list of ship_to options.
    Returns best match with confidence score.
    
    ship_to_options: list of dicts with 'ship_to', 'ship_to_name'
    """
    if not extracted_address or not ship_to_options:
        return None, 0.0
    
    extracted_lower = extracted_address.lower()
    best_match = None
    best_score = 0.0
    
    for option in ship_to_options:
        ship_to_name = option.get("ship_to_name", "")
        if not ship_to_name:
            continue
        
        ship_to_name_lower = ship_to_name.lower()
        
        # Simple substring matching
        # If extracted address contains the ship to name or vice versa
        if ship_to_name_lower in extracted_lower or extracted_lower in ship_to_name_lower:
            # Calculate rough confidence based on length overlap
            overlap_len = min(len(ship_to_name_lower), len(extracted_lower))
            max_len = max(len(ship_to_name_lower), len(extracted_lower))
            score = overlap_len / max_len if max_len > 0 else 0
            
            if score > best_score:
                best_score = score
                best_match = option
    
    return best_match, best_score

def repair_stebro_quantities(record):
    """
    Repair logic for Stebro files where Part No is often extracted as Quantity.
    Strategy: Look for 'Quantity Unit' pattern in raw text (e.g. '700,00 KG').
    """
    # 1. Check if Stebro
    src = str(record.get("source_file") or "").upper()
    vendor = str((record.get("header_fields") or {}).get("vendor_name") or "").upper()
    
    raw = str(record.get("raw_text") or "")
    is_stebro = "STEBRO" in src or "STEBRO" in vendor or ("STEBRO" in raw.upper())
    if not is_stebro or not raw:
        return record

    try:
        # Regex for European Quantity + Unit
        # Matches: 700,00 KG | 1.200,50 M | 100 PC
        # Group 1: Number part
        # Group 2: Unit part
        pat = r"(\d{1,3}(?:[.]\d{3})*(?:,\d+)?)\s*(KG|MT|MTON|TONNE|MTS|TON|PC|ST|M|L|PCE|PCS|BAG|ROL|T)\b"
        
        matches = re.findall(pat, raw, re.IGNORECASE)
        # matches list of tuples: [('700,00', 'KG'), ...]
        
        if not matches:
            return record
            
        print(f"  [Stebro Repair] Found candidates in text: {matches}")
        
        # We might have multiple matches. One for each line item?
        # Heuristic: Assign matches to line items from top to bottom.
        
        items = record.get("line_items", [])
        match_idx = 0
        
        for item in items:
            curr_qty = item.get("quantity")
            
            # Check if current quantity looks like a Part No (Integer, > 1000, no decimal details usually)
            # Or if it's just plainly wrong compared to our finding.
            # Stebro Part Nos like '5641'. Text has '700,00'.
            
            # If we run out of matches, stop
            if match_idx >= len(matches):
                break
                
            cand_qty_str, cand_unit = matches[match_idx]
            match_idx += 1
            
            # Simple override: If we found a candidate, assume extraction failed or is suspect.
            # Especially if current qty is None or different format.
            # But let's be slightly careful. 
            # If current qty is '700.0' and candidate is '700,00', it's same.
            
            # Just overwrite. The Regex with Unit is strong context.
            # But what if extracting Part No 5641 was correct?
            # Unlikely if '700,00 KG' is present. 5641 is likely Part No.
            
            print(f"  [Stebro Repair] Overwriting Qty '{curr_qty}' with '{cand_qty_str}' (Unit: {cand_unit})")
            item["quantity"] = cand_qty_str
            item["unit"] = cand_unit # Also fix unit if missing
            
    except Exception as e:
        print(f"  [Stebro Repair] Error: {e}")
        
    return record

def enrich_record(record):
    """
    Enriches a single extraction record with Sold To / Ship To data.
    Uses fuzzy matching to select the correct Ship To when multiple options exist.
    """
    header = record.get("header_fields", {})
    source_file = record.get("source_file", "")
    
    # -- STEP 0: Extract Sold-To ID directly from filename (most reliable source) --
    # Many filenames follow the pattern: "CompanyName (Sold-to 4020000777 - Material 123).PDF"
    import re as _re
    filename_sold_to = None
    m = _re.search(r'[Ss]old-?[Tt]o\s+(\d{7,12})', source_file)
    if m:
        filename_sold_to = m.group(1)
        print(f"  -> Filename Sold-To ID: {filename_sold_to}")
    
    # Try to find vendor/customer name
    # Prioritize customer_id_or_name because 'vendors' table likely contains Customer/Sold-To data
    vendor_name = (
        header.get("customer_id_or_name") or 
        header.get("vendor_name") or 
        header.get("supplier_name")
    )
    
    if not vendor_name:
        # Even without a vendor name, if we have a filename ID, apply it
        if filename_sold_to:
            header["sold_to_id"] = filename_sold_to
            header["customer_number"] = filename_sold_to
        print(f"  -> No vendor found for {source_file or 'unknown'}")
        return record
    
    # Custom Alias Map for specific overrides
    # Normalized Lowercase Input -> Official Master Data Name Pattern
    CUSTOM_ALIASES = {
        "niederwieser": "VF Verpackungen",  # Specific request
        "niederwieser gmbh": "VF Verpackungen",
    }
    
    vendor_lower = vendor_name.lower()
    for alias, target in CUSTOM_ALIASES.items():
        if alias in vendor_lower:
            print(f"  -> Applying Alias: '{vendor_name}' -> '{target}'")
            vendor_name = target
            break

    # Get master data
    master_data = get_vendor_master_data(vendor_name)
    
    if not master_data:
        print(f"  -> No master data for vendor: {vendor_name}")
        return record
    
    # Extract unique Sold To (should be 1 per vendor, but might be mixed legacy/new)
    sold_tos = list(set([m["sold_to"] for m in master_data if m["sold_to"]]))
    
    if sold_tos:
        # Prioritize 10-digit numeric IDs (e.g. 4020000777) over alphanumeric (E01514)
        def id_priority(sid):
            s = str(sid)
            is_num = s.isdigit()
            length = len(s)
            if is_num and length >= 7: return 2
            if is_num: return 1
            return 0
            
        sold_tos.sort(key=id_priority, reverse=True)
        best_id = sold_tos[0]
        
        # -- OVERRIDE: If filename has a Sold-To ID, it is the ground truth --
        if filename_sold_to and filename_sold_to in sold_tos:
            best_id = filename_sold_to
            print(f"  -> Using filename Sold-To ID: {best_id} (overrides fuzzy match: {sold_tos[0]})")
        elif filename_sold_to:
            # Filename ID not in fuzzy results, but still use it as it's more reliable
            best_id = filename_sold_to
            print(f"  -> Using filename Sold-To ID: {best_id} (not in fuzzy results)")
        
        header["sold_to_id"] = best_id
        header["customer_number"] = best_id
        
        # Overwrite extracted name with Official Master Data Name
        # Find the name corresponding to this ID (try best_id first, then any match)
        matched_name = None
        for m in master_data:
            if m["sold_to"] == best_id:
                matched_name = m["name"]
                break
        if not matched_name and master_data:
            matched_name = master_data[0]["name"]  # fallback to first match
        
        if matched_name:
            print(f"  -> Standardization: '{header.get('customer_id_or_name')}' -> '{matched_name}'")
            header["customer_id_or_name"] = matched_name
    
    # Extract Ship To options
    ship_to_options = [
        {"ship_to": m["ship_to"], "ship_to_name": m["ship_to_name"]} 
        for m in master_data if m["ship_to"]
    ]
    
    # If we have only one Ship To option, use it directly
    if len(ship_to_options) == 1:
        header["ship_to_id"] = ship_to_options[0]["ship_to"]
        header["ship_to_name"] = ship_to_options[0]["ship_to_name"]
        header["ship_to_selection_method"] = "single_option"
        print(f"  -> Enriched {record.get('source_file')}: Sold To={header.get('sold_to_id')}, Ship To={header['ship_to_id']}")
    
    elif len(ship_to_options) > 1:
        # Try fuzzy matching on extracted Ship To address
        extracted_ship_address = header.get("ship_to_address")
        
        if extracted_ship_address:
            best_match, confidence = fuzzy_match_ship_to(extracted_ship_address, ship_to_options)
            
            if best_match and confidence > 0.3:  # Threshold for acceptable match
                header["ship_to_id"] = best_match["ship_to"]
                header["ship_to_name"] = best_match["ship_to_name"]
                header["ship_to_selection_method"] = "fuzzy_match"
                header["ship_to_confidence"] = round(confidence, 2)
                print(f"  -> Matched Ship To for {record.get('source_file')} with confidence {confidence:.2f}")
            else:
                # Low confidence - use first as default
                header["ship_to_id"] = ship_to_options[0]["ship_to"]
                header["ship_to_name"] = ship_to_options[0]["ship_to_name"]
                header["ship_to_selection_method"] = "default_low_confidence"
                header["ship_to_confidence"] = round(confidence, 2) if confidence else 0.0
                header["review_required"] = True
                # Store all options for manual review
                header["ship_to_options"] = [f"{opt['ship_to']} - {opt['ship_to_name']}" for opt in ship_to_options[:5]]
                print(f"  -> Low confidence ({confidence:.2f}) for {record.get('source_file')} - review required")
        else:
            # No Ship To address extracted - use first as default
            header["ship_to_id"] = ship_to_options[0]["ship_to"]
            header["ship_to_name"] = ship_to_options[0]["ship_to_name"]
            header["ship_to_selection_method"] = "default_no_address"
            header["review_required"] = True
            header["ship_to_options"] = [f"{opt['ship_to']} - {opt['ship_to_name']}" for opt in ship_to_options[:5]]
            print(f"  -> No Ship To address extracted for {record.get('source_file')} - using default")
    
    header["customer_number"] = master_data[0].get("customer_number") if master_data else None
    header["master_data_source"] = "celonis"
    
    return record

def load_excel_mapping(excel_path):
    """
    Loads mapping rules from Excel:
    - Customer -> Sales Organization
    - Customer -> Order type
    - Customer -> One SO per PO (X)
    - (New) Customer + Customer Material -> Internal Material
    """
    if not os.path.exists(excel_path):
        print(f"Warning: Mapping Excel not found at {excel_path}")
        return {}, {}

    try:
        df = pd.read_excel(excel_path, dtype=str, engine='openpyxl')
        
        mapping = {}     # cust_id -> {sales_org, order_type, ...}
        material_map = {} # (cust_id, cust_mat) -> {"internal": internal_mat, "customer_mat": cust_mat}
        
        # Identify columns
        # Expected: 'Sales Organization', 'Customer', 'Material', 'Customer Material Number', 'Order type'
        # and 'One SO per PO...'
        
        so_col = [c for c in df.columns if "One SO per PO" in c]
        so_col_name = so_col[0] if so_col else None
        
        for _, row in df.iterrows():
            cust_id = str(row.get("Customer", "")).strip()
            if cust_id.endswith(".0"): cust_id = cust_id[:-2]
            
            if not cust_id or cust_id.lower() == "nan": continue
            
            # 1. General Customer Rules (store first occurrence)
            if cust_id not in mapping:
                mapping[cust_id] = {
                    "sales_organization": str(row.get("Sales Organization", "")).strip(),
                    "order_type": str(row.get("Order type", "")).strip(),
                    "so_grouping_rule": str(row.get(so_col_name, "")).strip() if so_col_name else ""
                }
            
            # 2. Material Mapping
            cust_mat = str(row.get("Customer Material Number", "")).strip()
            internal_mat = str(row.get("Material", "")).strip()
            
            if cust_mat and internal_mat and cust_mat.lower() != "nan" and internal_mat.lower() != "nan":
                 # Clean up potential float strings like "123.0"
                 if cust_mat.endswith(".0"): cust_mat = cust_mat[:-2]
                 if internal_mat.endswith(".0"): internal_mat = internal_mat[:-2]
                 
                 # Store both the customer material code AND the internal material number
                 material_map[(cust_id, cust_mat)] = {"internal": internal_mat, "customer_mat": cust_mat}

        print(f"Loaded {len(mapping)} customer rules and {len(material_map)} material mappings from Excel.")
        return mapping, material_map

    except Exception as e:
        print(f"Error loading Excel mapping: {e}")
        return {}, {}

def convert_to_excel(records, output_path):
    """
    Convert enriched JSON records to an Excel file with flattened structure.
    """
    rows = []
    
    for rec in records:
        header = rec.get("header_fields", {})
        line_items = rec.get("line_items", [])
        
        if not line_items:
            rows.append({
                "PO Number": header.get("po_number"),
                "Order Date": header.get("order_date"),
                "Requested Delivery Date": header.get("requested_delivery_date"),
                "Customer Name": header.get("customer_id_or_name"),
                "Vendor Name": header.get("vendor_name"),
                "Sold To ID": header.get("sold_to_id"),
                "Sales Org": header.get("sales_organization"),
                "Order Type": header.get("order_type"),
                "SO Rule": header.get("so_grouping_rule"),
                "Ship To ID": header.get("ship_to_id"),
                "Ship To Name": header.get("ship_to_name"),
                "Customer Number": header.get("customer_number"),
                "Material Description": None,
                "Quantity": None,
                "Unit": None,
                "Delivery Date": None,
                "Extracted Material Code": None,
                "Customer Material Number": None,
                "Internal Material Number": None,
                "Source File": rec.get("source_file")
            })
        else:
            for item in line_items:
                rows.append({
                    "PO Number": header.get("po_number"),
                    "Order Date": header.get("order_date"),
                    "Requested Delivery Date": header.get("requested_delivery_date"),
                    "Customer Name": header.get("customer_id_or_name"),
                    "Vendor Name": header.get("vendor_name"),
                    "Sold To ID": header.get("sold_to_id"),
                    "Sales Org": header.get("sales_organization"),
                    "Order Type": header.get("order_type"),
                    "SO Rule": header.get("so_grouping_rule"),
                    "Ship To ID": header.get("ship_to_id"),
                    "Ship To Name": header.get("ship_to_name"),
                    "Customer Number": header.get("customer_number"),
                    "Material Description": item.get("material_description"),
                    "Quantity": item.get("quantity"),
                    "Unit": item.get("unit"),
                    "Delivery Date": item.get("delivery_date"),
                    "Extracted Material Code": item.get("material_code"),          # Raw code from PO PDF
                    "Customer Material Number": item.get("customer_material_number"), # Exact Excel 'Customer Material Number'
                    "Internal Material Number": item.get("internal_material"),        # Excel 'Material' (Envalior internal)
                    "Source File": rec.get("source_file")
                })
    
    df = pd.DataFrame(rows)
    df.to_excel(output_path, index=False, engine='openpyxl')
    print(f"\\nExcel file saved to: {output_path}")

def generate_so_groups(enriched_records):
    """
    Groups line items into Sales Orders based on so_grouping_rule.

    Rules (from Excel mapping):
      X = One SO per PO   → all line items from the PO go into ONE sales order
      O = Separate SO per PO item → each line item gets its OWN sales order

    Output: list of SO dicts, each with:
      - so_number (sequential within this run)
      - grouping_rule (X or O)
      - po_number
      - customer_id
      - sales_organization
      - order_type
      - source_file
      - line_items (list)
    """
    so_list = []
    so_counter = 1

    for rec in enriched_records:
        header = rec.get("header_fields") or {}
        rule = header.get("so_grouping_rule", "").strip().upper()
        po_number = header.get("po_number") or header.get("order_number", "")
        cust_id = header.get("sold_to_id") or header.get("customer_number", "")
        sales_org = header.get("sales_organization", "")
        order_type = header.get("order_type", "")
        source_file = rec.get("source_file", "")
        items = rec.get("line_items", [])

        if not items:
            continue

        if rule == "X":
            # -- X: ONE SO for ALL line items in this PO --
            so_list.append({
                "so_number": f"SO-{so_counter:04d}",
                "grouping_rule": "X - One SO per PO",
                "po_number": po_number,
                "customer_id": cust_id,
                "sales_organization": sales_org,
                "order_type": order_type,
                "source_file": source_file,
                "line_items": items
            })
            print(f"  [SO Group X] PO={po_number} → SO-{so_counter:04d} ({len(items)} items combined)")
            so_counter += 1

        elif rule == "O":
            # -- O: SEPARATE SO for EACH line item --
            for item in items:
                so_list.append({
                    "so_number": f"SO-{so_counter:04d}",
                    "grouping_rule": "O - Separate SO per Item",
                    "po_number": po_number,
                    "customer_id": cust_id,
                    "sales_organization": sales_org,
                    "order_type": order_type,
                    "source_file": source_file,
                    "line_items": [item]
                })
                print(f"  [SO Group O] PO={po_number} → SO-{so_counter:04d} (item: {item.get('material_code', '?')})")
                so_counter += 1

        else:
            # -- No rule: SKIP (only X and O create SOs) --
            print(f"  [SO Group] PO={po_number} skipped — no X/O rule found.")

    return so_list


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=str, default="results_paddle.jsonl")
    parser.add_argument("--output", type=str, default="enriched_results.json")
    parser.add_argument("--excel", type=str, default="enriched_results.xlsx")
    parser.add_argument("--mapping", type=str, default="EXPORT_20260216_100124.XLSX", help="Mapping Excel file")
    args = parser.parse_args()
    
    # Load Knowledge Base
    load_all_vendors()
    
    # Load Excel Rules
    excel_rules, material_map = load_excel_mapping(args.mapping)
    
    # Read input
    print(f"Reading {args.input}...")
    records = []
    with open(args.input, 'r', encoding='utf-8') as f:
        for line in f:
            if line.strip():
                try:
                    records.append(json.loads(line))
                except: pass
    
    print(f"Processing {len(records)} records...")
    
    # Enrich
    enriched = []
    for rec in records:
        # 0. Pre-Enrichment Repair (Stebro)
        rec = repair_stebro_quantities(rec)

        # 1. Vendor/Ship-To Enrichment (KB)
        rec = enrich_record(rec)
        
        # 2. Excel Rule Application
        header = rec.get("header_fields", {})
        source_file = rec.get("source_file", "")
        
        def apply_excel_rule(id_value):
            """Try to find and apply an Excel rule for a given ID. Returns ID used if applied, else None."""
            if not id_value:
                return None
            sid = str(id_value).strip()
            # Direct match
            rule = excel_rules.get(sid)
            used_id = sid
            
            if not rule:
                # Try stripping leading zeros
                sid_stripped = sid.lstrip("0")
                rule = excel_rules.get(sid_stripped)
                used_id = sid_stripped
                
            if rule:
                header["sales_organization"] = rule["sales_organization"]
                header["order_type"] = rule["order_type"]
                header["so_grouping_rule"] = rule["so_grouping_rule"]
                
                # Special check for "One SO per PO" (X)
                if rule["so_grouping_rule"] == "X":
                    print(f"  -> !!! SPECIAL RULE: One SO per PO (X) found for {used_id} !!!")
                
                print(f"  -> Excel Rule applied for ID {used_id}: SalesOrg={rule['sales_organization']}, OrderType={rule['order_type']}, SORule={rule['so_grouping_rule']}")
                return used_id
            return None
        
        # Try IDs in priority order: sold_to_id -> customer_number -> ship_to_id
        matched_customer_id = apply_excel_rule(header.get("sold_to_id"))
        if not matched_customer_id:
            matched_customer_id = apply_excel_rule(header.get("customer_number"))
        if not matched_customer_id:
            matched_customer_id = apply_excel_rule(header.get("ship_to_id"))
            
        if not matched_customer_id and header.get("sold_to_id"):
            print(f"  -> No Excel rule found for IDs: sold_to={header.get('sold_to_id')}, cust_num={header.get('customer_number')}")
        
        # 3. Material Mapping
        # Use the matched_customer_id if available, otherwise fallback to extracted IDs

        # --- NEW LOGIC: Extract Sold-To from filename ---
        filename_sold_to = None
        match = re.search(r"Sold-to\s+(\d+)", source_file, re.IGNORECASE)
        if match:
            filename_sold_to = match.group(1)
            # Normalize to match Excel (remove leading zeros? check later)
            # Excel seems to have '4020001301', which is 10 digits.
            # Filename might have '0040...' or '40...'.
            # Taking raw digits is safest if Excel matches.
            pass
        
        cust_id = filename_sold_to if filename_sold_to else (
            header.get("customer_number") or 
            header.get("sold_to_id") or 
            header.get("customer_id_or_name")
        )

        # If we found a Sold-to in filename, apply rules if any key header field is missing
        if filename_sold_to and not header.get("so_grouping_rule"):
            print(f"  -> Applying Excel Rule from Filename Sold-to: {filename_sold_to}")
            apply_excel_rule(filename_sold_to)
        
        if cust_id:
            cust_mat_keys = {k[1]: v for k, v in material_map.items()
                             if k[0] == cust_id or k[0] == cust_id.lstrip("0")}

            # --- NEW LOGIC: Extract Material from filename ---
            filename_material = None
            mat_match = re.search(r"Material\s+(\w+)", source_file, re.IGNORECASE)
            if mat_match:
                filename_material = mat_match.group(1)
            
            # If line items are empty but we have info from filename, synthesize an item
            if not rec.get("line_items") and filename_material:
                print(f"  -> Synthesizing line item from filename: Mat={filename_material}")
                rec["line_items"] = [{
                    "material_code": filename_material,
                    "description": "Extracted from Filename",
                    "quantity": None,
                    "unit": None
                }]

            for item in rec.get("line_items", []):
                # Apply filename material if item material is missing
                if not item.get("material_code") and filename_material:
                    item["material_code"] = filename_material
                # Fix: Normalize Quantity (handle European comma decimals)
                if "quantity" in item:
                    item["quantity"] = normalize_quantity_european(item["quantity"])

                cust_mat = item.get("material_code")
                if cust_mat:
                    # Clean extracted material (trim)
                    cust_mat_clean = str(cust_mat).strip()
                    if cust_mat_clean.endswith(".0"): cust_mat_clean = cust_mat_clean[:-2]
                    
                    internal_mat = None
                    matched_cust_mat = None  # The exact Excel Customer Material Number key
                    match_method = None

                    # Strategy 1: Exact match
                    if cust_mat_clean in cust_mat_keys:
                        result = cust_mat_keys[cust_mat_clean]
                        internal_mat = result["internal"]
                        matched_cust_mat = result["customer_mat"]
                        match_method = "exact"

                    # Strategy 2: Excel key starts with extracted code
                    # e.g. extracted='111102' matches Excel='111102 OKTABIN'
                    if not internal_mat and cust_mat_clean:
                        for excel_mat, result in cust_mat_keys.items():
                            if excel_mat.startswith(cust_mat_clean):
                                internal_mat = result["internal"]
                                matched_cust_mat = result["customer_mat"]
                                match_method = f"prefix(excel={excel_mat})"
                                break

                    # Strategy 3: Case-insensitive exact match
                    # e.g. extracted='Akulon F130-C2' matches Excel='AKULON F130-C2'
                    if not internal_mat and cust_mat_clean:
                        cust_mat_upper = cust_mat_clean.upper()
                        for excel_mat, result in cust_mat_keys.items():
                            if excel_mat.upper() == cust_mat_upper:
                                internal_mat = result["internal"]
                                matched_cust_mat = result["customer_mat"]
                                match_method = f"case-insensitive(excel={excel_mat})"
                                break

                    # Strategy 4: Substring match - remove leading characters
                    # e.g. extracted='PA-159' matches Excel='8PA-159'
                    if not internal_mat and cust_mat_clean:
                        cust_mat_upper = cust_mat_clean.upper().lstrip("0")
                        for excel_mat, result in cust_mat_keys.items():
                            excel_upper = excel_mat.upper()
                            if cust_mat_upper and (cust_mat_upper in excel_upper or excel_upper.rstrip(" ").endswith(cust_mat_upper)):
                                internal_mat = result["internal"]
                                matched_cust_mat = result["customer_mat"]
                                match_method = f"substring(excel={excel_mat})"
                                break

                    # Strategy 5: Numeric prefix truncation
                    # e.g. extracted='11110201' -> try '111102' -> matches Excel='111102 OKTABIN'
                    # Also handles codes like '086711300 OCTA' where extracted is '0867113'
                    if not internal_mat and cust_mat_clean and cust_mat_clean[:1].isdigit():
                        trial = cust_mat_clean
                        while len(trial) >= 4:
                            trial = trial[:-1]  # strip one trailing character
                            for excel_mat, result in cust_mat_keys.items():
                                if excel_mat.startswith(trial) or excel_mat.upper().startswith(trial.upper()):
                                    internal_mat = result["internal"]
                                    matched_cust_mat = result["customer_mat"]
                                    match_method = f"numeric-prefix-trunc(trial={trial}, excel={excel_mat})"
                                    break
                            if internal_mat:
                                break

                    # Strategy 6: Extracted code contains Excel key as substring
                    # e.g. extracted='F223-200166300' contains Excel key '200166300'
                    if not internal_mat and cust_mat_clean:
                        cust_upper = cust_mat_clean.upper()
                        for excel_mat, result in cust_mat_keys.items():
                            ek = excel_mat.upper().split()[0]  # take first word of Excel key
                            if len(ek) >= 5 and ek in cust_upper:
                                internal_mat = result["internal"]
                                matched_cust_mat = result["customer_mat"]
                                match_method = f"excel-key-in-extracted(excel={excel_mat})"
                                break

                    if internal_mat:
                        # Store BOTH the exact Excel Customer Material Number AND the Internal Material Number
                        item["customer_material_number"] = matched_cust_mat   # Excel 'Customer Material Number' column
                        item["internal_material"] = internal_mat               # Excel 'Material' column
                        print(f"  -> Material Mapped [{match_method}]: {cust_mat_clean} -> cust_mat={matched_cust_mat}, internal={internal_mat} (Cust: {cust_id})")
                    else:
                        item["customer_material_number"] = None
                        item["internal_material"] = None
                        print(f"  -> Material NOT found: cust_mat={cust_mat_clean!r} for cust_id={cust_id} (excel keys: {list(cust_mat_keys.keys())[:3]})")
        
        enriched.append(rec)
    
    # -- Step 4: SO Grouping --------------------------------------------------
    so_groups = generate_so_groups(enriched)
    so_groups_path = args.output.replace(".json", "_so_groups.json")
    with open(so_groups_path, 'w', encoding='utf-8') as f:
        json.dump(so_groups, f, indent=2, ensure_ascii=False)
    print(f"\nSO Groups saved to: {so_groups_path} ({len(so_groups)} SOs)")
    
    # Save JSON
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(enriched, f, indent=2, ensure_ascii=False)
    
    print(f"\nEnriched JSON saved to: {args.output}")
    
    # Save Excel
    if args.excel:
        convert_to_excel(enriched, args.excel)

if __name__ == "__main__":
    main()
