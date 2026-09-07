"""
PO OCR Script - Tesseract variant (with LLM extraction)
-------------------------------------------------------

This script runs Tesseract OCR on your PO images to get raw text,
then uses a local LLM (via LangChain + Transformers) to structure
that text into JSON according to a schema.

Run: python po_extraction_tesseract.py --folder test_data --schema po_schema.json
"""

import argparse
import json
import os
os.environ['DISABLE_MODEL_SOURCE_CHECK'] = 'True'

import shutil
import sys
import re
from pathlib import Path


from PIL import Image
import pytesseract

# Enforce imports
print("DEBUG: Importing LLM dependencies...", flush=True)
from langchain_community.llms import HuggingFacePipeline
from transformers import AutoTokenizer, AutoModelForCausalLM, pipeline, BitsAndBytesConfig
import torch
print("DEBUG: Dependencies imported successfully.", flush=True)

LLM_AVAILABLE = True



# ------------------------------------------------
# CONFIG
# ------------------------------------------------

# Check/Setup Tesseract Path
tesseract_cmd = shutil.which("tesseract")
if not tesseract_cmd:
    default_path = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    if os.path.exists(default_path):
        pytesseract.pytesseract.tesseract_cmd = default_path
        print(f"Index: Tesseract found at {default_path}")
    else:
        print("Warning: Tesseract not found in PATH or standard location.")
        # We allow it to fail later if actually needed
else:
    print(f"Index: Tesseract found in PATH at {tesseract_cmd}")


# Model Init
MODEL_ID = "Qwen/Qwen2.5-3B-Instruct" 
DEVICE = "cuda" if (LLM_AVAILABLE and torch.cuda.is_available()) else "cpu"

EXTRACTION_SCHEMA = """
{
  "header_fields": {
    "customer_id_or_name": null,
    "requested_delivery_date": null,
    "po_number": null,
    "order_date": null
  },
  "line_items": [
    {
      "material_description": null,
      "quantity": null,
      "delivery_date": null,
      "material_code": null,
      "price": null
    }
  ],
  "summary": {
    "detected_language": null,
    "confidence_score": null
  }
}
"""

PROMPT_TEMPLATE = """You are a helpful assistant that extracts data from documents.
I will provide a SCHEMA and a DOCUMENT TEXT.
Your job is to extract values from the text and fit them into the SCHEMA.

RULES:
- Return valid JSON only.
- Fill in the values based on the text.
- If a value is missing, use null.

EXAMPLE INPUT:
Text: "PURCHASE ORDER #99999. Date: 2024-01-01. Cust: ABC Corp."
Schema: {{ "header_fields": {{ "po_number": null, "date": null, "customer": null }} }}

EXAMPLE OUTPUT:
{{ "header_fields": {{ "po_number": "99999", "date": "2024-01-01", "customer": "ABC Corp" }} }}

***

REAL TASK:

SCHEMA:
{extra_schema}

DOCUMENT TEXT:
{content}

JSON OUTPUT:
"""


# ------------------------------------------------
# LLM Setup
# ------------------------------------------------

llm_pipeline = None

def setup_llm():
    global llm_pipeline
    print("DEBUG: Entering setup_llm...", flush=True)
    
    print(f"Loading model: {MODEL_ID} on {DEVICE} (using 4-bit quantization)...", flush=True)
    try:
        tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)

        # Quantization Config
        quantization_config = None

        model = AutoModelForCausalLM.from_pretrained(
            MODEL_ID,
            quantization_config=quantization_config,
            device_map="auto"
        )

        pipe = pipeline(
            "text-generation",
            model=model,
            tokenizer=tokenizer,
            max_new_tokens=512,
            temperature=0.01,
            do_sample=True
            )
        llm_pipeline = HuggingFacePipeline(pipeline=pipe)
        print("LLM loaded successfully.")
    except Exception as e:
        print(f"Error loading LLM: {e}")
        llm_pipeline = None


# ------------------------------------------------
# Helpers
# ------------------------------------------------

def _extract_json_from_text(text: str) -> dict:
    if not isinstance(text, str):
        return {}
    
    cleaned = text.strip()
    # Remove markdown code blocks
    cleaned = re.sub(r"```json\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"```\s*", "", cleaned)
    
    start = cleaned.find("{")
    if start == -1:
         # Try finding array if schema expects array, but here we expect dict
        return {"error": "No JSON start found", "raw": text[:200]}

    # Robust extraction by counting braces
    balance = 0
    end = -1
    for i, char in enumerate(cleaned[start:], start=start):
        if char == "{":
            balance += 1
        elif char == "}":
            balance -= 1
            if balance == 0:
                end = i
                break
    
    if end != -1:
        candidate = cleaned[start : end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as e:
            return {"error": f"JSON parse error: {e}", "raw": candidate}
    
    # Fallback: Try regex or aggressive finding of last }
    end_fallback = cleaned.rfind("}")
    if end_fallback > start:
         candidate = cleaned[start : end_fallback + 1]
         try:
            return json.loads(candidate)
         except:
            pass
            
    return {"error": "Could not extract valid JSON", "raw": text[:500]}


def ocr_image_to_text(path: Path) -> str:
    """Run Tesseract OCR on a single image and return the text."""
    try:
        img = Image.open(path)
        text = pytesseract.image_to_string(img, lang="eng")
        return text.strip()
    except Exception as e:
        print(f"Error processing {path}: {e}")
        return ""


def main():
    parser = argparse.ArgumentParser(description="Run Tesseract OCR + LLM Extraction")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", type=str, help="Path to single image")
    group.add_argument("--folder", type=str, help="Folder of images")
    parser.add_argument("--output", type=str, default="results_tesseract.jsonl", help="Output JSONL")
    parser.add_argument("--schema", type=str, help="Path to custom JSON schema")

    args = parser.parse_args()

    # Load Schema
    current_schema = EXTRACTION_SCHEMA
    if args.schema:
        try:
            current_schema = Path(args.schema).read_text(encoding="utf-8")
            print(f"Loaded schema from {args.schema}")
        except Exception as e:
            print(f"Error loading schema: {e}. Using default.")

    # Init LLM
    setup_llm()

    # Gather files
    input_path = Path(args.file or args.folder)
    files_to_process = []
    if input_path.is_file():
        files_to_process.append(input_path)
    elif input_path.is_dir():
        exts = {".png", ".jpg", ".jpeg", ".tiff", ".bmp"}
        files_to_process = sorted([
            p for p in input_path.iterdir() 
            if p.is_file() and p.suffix.lower() in exts
        ])
    else:
        print("Invalid input path")
        sys.exit(1)

    results = []

    print(f"Processing {len(files_to_process)} files...")

    for fpath in files_to_process:
        print(f"Extracting {fpath.name}...")
        
        # 1. OCR
        raw_text = ocr_image_to_text(fpath)
        if not raw_text:
            print("  -> Empty OCR text, skipping LLM.")
            rec = {
                "source_file": fpath.name, 
                "error": "Empty OCR text"
            }
            results.append(rec)
            continue
        
        print(f"DEBUG: OCR Text Length: {len(raw_text)}")
        print(f"DEBUG: OCR Text Preview: {raw_text[:200]}...")

        # 2. LLM Extraction
        extracted_data = {}
        if llm_pipeline:
            prompt = PROMPT_TEMPLATE.format(extra_schema=current_schema, content=raw_text)
            try:
                # Invoke LLM
                print("DEBUG: Invoking LLM...", flush=True)
                response_str = llm_pipeline.invoke(prompt)
                print(f"DEBUG: LLM Response Preview: {response_str[:500]}...", flush=True)
                extracted_data = _extract_json_from_text(response_str)
            except Exception as e:
                print(f"  -> LLM Error: {e}")
                extracted_data = {"error": f"LLM extraction failed: {str(e)}", "raw_text": raw_text[:200]}
        else:
            extracted_data = {"error": "LLM not initialized", "raw_text": raw_text}

        # Merge meta
        extracted_data["source_file"] = fpath.name
        extracted_data["ocr_engine"] = "tesseract"
        
        results.append(extracted_data)

    # Save
    out_path = Path(args.output)
    with out_path.open("w", encoding="utf-8") as f:
        for rec in results:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"Done! Saved to {out_path}")
    if results:
        print("Sample result:")
        print(json.dumps(results[0], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
