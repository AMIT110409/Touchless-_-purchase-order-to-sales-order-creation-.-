"""
Smart PO Extraction Pipeline
----------------------------
1. Input Analysis: Checks if digital PDF (fast text) or Scanned (OCR).
2. Extraction: Uses Qwen 2.5 LLM to extract JSON.
3. Validation: Queries 'knowledge_base.db' (SQLite) to validate/correct Vendor and PO Number.

Usage:
  python smart_po_extraction.py --folder test_data
"""

import argparse
import json
import os
os.environ['DISABLE_MODEL_SOURCE_CHECK'] = 'True'

import re
import sys
import sys
from sqlalchemy import create_engine, text
import pandas as pd
import torch
import fitz # PyMuPDF
from pathlib import Path
from transformers import AutoTokenizer, AutoModelForCausalLM, pipeline, BitsAndBytesConfig
from langchain_community.llms import HuggingFacePipeline
import subprocess

# ------------------------------------------------
# CONFIG
# ------------------------------------------------
MODEL_ID = "Qwen/Qwen2.5-3B-Instruct"
MODEL_ID = "Qwen/Qwen2.5-3B-Instruct"
DB_CONNECTION_STRING = os.getenv("DB_CONNECTION_STRING")
DB_PATH = os.getenv("DB_PATH", "knowledge_base.db")
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

if DB_CONNECTION_STRING:
    db_engine = create_engine(DB_CONNECTION_STRING)
else:
    db_engine = create_engine(f"sqlite:///{DB_PATH}")

PROMPT_TEMPLATE = """You are an AI assistant for Purchase Order data extraction. 
Extract the following fields from the document text into JSON format.

RULES:
1. **Customer**: The buyer/billed-to entity.
2. **Vendor**: The supplier/seller entity. 
   - I have a database of vendors. Try to match the name exactly if possible.
3. **PO Number**: Unique order number. 
   - ABB usually has 7 digits (e.g., 1263152).
   - Legrand starts with P (e.g., P176476).
4. **Order Date**: Date the order was placed.
5. **Requested Date**: Date delivery is expected.
6. **Line Items**: List of items with Material Code, Description, Quantity, Unit.
   - Separate Quantity and Unit (e.g., "100 kg" -> Qty: 100, Unit: kg).

SCHEMA:
{{
  "header": {{ "generated_po_number": null, "order_date": null, "requested_date": null, "vendor_name": null, "customer_name": null }},
  "items": [ {{ "material_code": null, "description": null, "quantity": null, "unit": null }} ]
}}

DOCUMENT TEXT:
{content}

JSON OUTPUT:
```json
"""

llm_pipeline = None

# ------------------------------------------------
# 1. KNOWLEDGE BASE LOOKUP
# ------------------------------------------------
def fuzzy_search_vendor(name_fragment):
    """
    Finds the closest vendor match in the DB.
    Refines extraction by validating against master data.
    """
    if not name_fragment or len(name_fragment) < 3:
        return None
        
    with db_engine.connect() as conn:
        # Simple LIKE search
        query = text("SELECT name, customer_number FROM vendors WHERE name LIKE :name_pattern LIMIT 1")
        res = conn.execute(query, {"name_pattern": f"%{name_fragment}%"}).fetchone()
    
    if res:
        return {"name": res[0], "id": res[1]}
    return None

def validate_po_number(po_number, vendor_name): 
    """
    Checks if PO number matches known patterns for the vendor.
    (Placeholder logic - to be refined with real patterns from DB if stored)
    """
    if not po_number: return None
    
    # ABB Rule
    if vendor_name and "ABB" in vendor_name.upper():
        # Expect 7 digits
        # If we got something else, maybe the OCR missed the real one?
        if not re.match(r"^\d{7}$", str(po_number)):
             return {"valid": False, "reason": "Expected 7 digits for ABB"}
             
    return {"valid": True}

# ------------------------------------------------
# 2. INPUT PROCESSING (Hybrid)
# ------------------------------------------------
def extract_text_from_pdf(pdf_path):
    """
    Tries to extract text directly. Returns text on success.
    Returns None if text is insufficient (scanned).
    """
    try:
        doc = fitz.open(pdf_path)
        text = ""
        for page in doc:
            text += page.get_text()
            
        if len(text.strip()) > 50: # Threshold for "Digital PDF"
            return text
    except Exception as e:
        print(f"  -> PDF Text Error: {e}")
        pass
    return None

def extract_text_via_ocr(file_path):
    """
    Falls back to run_ocr_tool.py (PaddleOCR via subprocess).
    """
    print("  -> Falling back to OCR...")
    import sys
    cmd = [sys.executable, "run_ocr_tool.py", "--file", str(file_path)]
    res = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if res.returncode == 0:
        return res.stdout
    else:
        print(f"  -> OCR Failed: {res.stderr}")
        return ""

# ------------------------------------------------
# 3. LLM SETUP
# ------------------------------------------------
def setup_llm():
    global llm_pipeline
    print(f"Loading {MODEL_ID} on {DEVICE}...")
    
    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_compute_dtype=torch.float16,
        bnb_4bit_quant_type="nf4"
    )

    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
    model = AutoModelForCausalLM.from_pretrained(
        MODEL_ID, 
        quantization_config=bnb_config, 
        device_map="auto"
    )

    pipe = pipeline(
        "text-generation", 
        model=model, 
        tokenizer=tokenizer, 
        max_new_tokens=2048, 
        temperature=0.01, # Greedy for extraction
        return_full_text=False
    )
    llm_pipeline = HuggingFacePipeline(pipeline=pipe)

def parse_json(text):
    # Extract JSON content
    try:
        match = re.search(r"```json(.*?)```", text, re.DOTALL)
        if match:
            return json.loads(match.group(1))
        # Fallback
        start = text.find("{")
        end = text.rfind("}") + 1
        return json.loads(text[start:end])
    except:
        return None

# ------------------------------------------------
# MAIN FLOW
# ------------------------------------------------
def process_file(fpath):
    print(f"Processing: {fpath.name}")
    
    # 1. Hybrid Input
    raw_text = None
    if str(fpath).lower().endswith(".pdf"):
        raw_text = extract_text_from_pdf(fpath)
        if raw_text:
             print("  -> Method: Digital PDF (Fast)")
    
    if not raw_text: 
        # Image or Scanned PDF
        print("  -> Method: OCR (Visual)")
        raw_text = extract_text_via_ocr(fpath)
        
    if not raw_text or len(raw_text.strip()) == 0:
        return {"error": "No text extracted"}

    # 2. Extraction
    try:
        if llm_pipeline:
            prompt = PROMPT_TEMPLATE.format(content=raw_text[:4000]) # truncated context
            res = llm_pipeline.invoke(prompt)
            # Inspect output type
            print(f"    [DEBUG] LLM Response Type: {type(res)}")
            if hasattr(res, 'content'): res = res.content
            
            data = parse_json(res)
            
            if not data:
                print(f"    [DEBUG] Failed to parse. Raw:\n{res}\n")
                return {"error": "Failed to parse JSON", "raw": str(res)}
        else:
            return {"error": "LLM not loaded"}
    except Exception as e:
        print(f"    [ERROR] Exception during LLM/Parse: {e}")
        return {"error": str(e)}
        
    # 3. Validation (Knowledge Base)
    header = data.get("header", {})
    vendor_name = header.get("vendor_name")
    
    # Correct Vendor Name
    if vendor_name:
        match = fuzzy_search_vendor(vendor_name)
        if match:
            print(f"  -> KB Correction: '{vendor_name}' matched to '{match['name']}' ({match['id']})")
            header["vendor_name"] = match["name"]
            header["vendor_id"] = match["id"]
            
            # Validate PO based on vendor logic?
            # (See validate_po_number function placeholder)

    return data

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--folder", type=str, required=True)
    args = parser.parse_args()
    
    p = Path(args.folder)
    files = list(p.glob("*.*")) # PDF, PNG, etc.
    
    setup_llm()
    
    results = []
    for f in files:
        # Simple filter
        if f.suffix.lower() not in ['.pdf', '.png', '.jpg', '.jpeg']: continue
        
        try:
            res = process_file(f)
            res["filename"] = f.name
            results.append(res)
        except Exception as e:
            print(f"Error file {f.name}: {e}")
            
    with open("smart_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print("Done. Saved to smart_results.json")

if __name__ == "__main__":
    main()
