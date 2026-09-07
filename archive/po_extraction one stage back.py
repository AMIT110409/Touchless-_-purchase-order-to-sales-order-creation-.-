"""
PO Extraction Script - PaddleOCR (Subprocess) + LangChain
---------------------------------------------------------
Uses run_ocr_tool.py (OCR) + Local LLM (Structure)
- Model: Qwen/Qwen2.5-3B-Instruct (4-bit quantized)
- Prompt: One-shot example for robustness

Run: python po_extraction_paddle_langchain.py --folder test_data --schema po_schema.json
"""

import argparse
import json
import os
os.environ['DISABLE_MODEL_SOURCE_CHECK'] = 'True'

import re
import sys
import subprocess
from pathlib import Path

# Move torch/transformers to top
import torch
from transformers import AutoTokenizer, AutoModelForCausalLM, pipeline, BitsAndBytesConfig
from langchain_community.llms import HuggingFacePipeline

# Note: PaddleOCR is NOT imported here to avoid DLL conflicts.
# We call run_ocr_tool.py via subprocess.

# ------------------------------------------------
# CONFIG
# ------------------------------------------------

MODEL_ID = "Qwen/Qwen2.5-3B-Instruct" 
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

PROMPT_TEMPLATE = """You are a specialized document extraction system.
Your task is to extract structured data from the provided document text into JSON format.

INSTRUCTIONS:
1. **Analyze Language**: Identify the document language.
2. **Identify Participants (Purchase Order Logic)**:
   - **Customer (Buyer)**: The entity *placing* the order. Look for 'Buyer', 'Bill To', 'Invoice To', or the main logo at the top left/right.
   - **Vendor (Seller)**: The entity *receiving* the order. Look for 'To:', 'Vendor:', 'Supplier:', 'Spett.'.
   - *Example*: If header says 'ABB' and addresses 'Envalior', Customer is ABB, Vendor is Envalior.
3. **Locate Keys**:
   - **PO Number**: Look for 'Order No', 'Ordine N.', 'Commande', 'PO#', 'Purchase Order'.
     - **Warning**: Do not confuse 'Vendor No', 'VAT Number', or 'Code' with PO Number.
     - **Warning**: If you see a very long mixed string (e.g., '40220GS232819270'), it is likely a combination of IDs (Vendor+VAT). Search for a separate, shorter structural number (e.g., '1263152') found near 'Order', 'PO', or top codes.
   - **Dates**:
     - **Order Date**: The date the document was generated. Usually labeled 'Date', 'Order Date', 'Data Ordine'.
     - **Delivery/Req Date**: Labeled 'Delivery Date', 'Requested Date', 'Due Date', 'Data Consegna'.
     - *Critical*: Do not conflate multiple dates. If only one date exists near the PO number, it is usually the Order Date.
4. **Line Items**:
   - **Quantity**: The numeric amount ordered.
     - **Critical**: Do NOT extract 'Pos.Nr', 'Line No', or 'Item No' (e.g., 1, 10, 0010) as Quantity.
     - Look for 'Qty', 'Quantity', 'Menge', 'Qta', or numbers in the line item row.
   - **Unit**: Extract the unit of measure (e.g., 'kg', 'm', 'pcs', 'BAG').
   - **Description**: Material description.
   - **Material Code**: The product code/ID.
5. **Reasoning**: Explain your logic (e.g., "Found 'ABB' in header -> Customer").
6. OUTPUT RAW JSON ONLY.

### EXAMPLE
SCHEMA:
{{
  "header_fields": {{ "po_number": null, "order_date": null, "requested_delivery_date": null, "customer_id_or_name": null }},
  "line_items": [ {{ "material_description": null, "quantity": null, "unit": null, "delivery_date": null, "material_code": null }} ]
}}

DOCUMENT TEXT:
HEADER_LOGO: XYZ Corp
To: ABC Supplies
P.O. No: 999-A
Date: 01/01/2024
Req Del: 02/02/2024
Item 10: Widget A   Qty: 500 pcs

JSON OUTPUT:
{{
  "reasoning": "Header is 'XYZ Corp' -> Customer. Addressed to 'ABC Supplies' -> Vendor. Found 'P.O. No: 999-A'. Date 01/01 is Order, 02/02 is Req Del.",
  "data": {{
      "header_fields": {{ "po_number": "999-A", "order_date": "01/01/2024", "requested_delivery_date": "02/02/2024", "customer_id_or_name": "XYZ Corp" }},
      "line_items": [ {{ "material_description": "Widget A", "quantity": "500", "unit": "pcs", "delivery_date": null, "material_code": null }} ]
  }}
}}
### END EXAMPLE

### REAL TASK
SCHEMA:
{extra_schema}

DOCUMENT TEXT:
{content}

JSON OUTPUT:
```json
"""

# ------------------------------------------------
# LLM Setup
# ------------------------------------------------
llm_pipeline = None

def setup_llm():
    global llm_pipeline
    print(f"Loading model: {MODEL_ID} on {DEVICE} ...")
    
    try:
        quantization_config = None
        if DEVICE == "cuda":
            # Use 4-bit quantization
            quantization_config = None

        tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
        
        load_kwargs = {
            "device_map": "auto",
        }
        if quantization_config:
            load_kwargs["quantization_config"] = quantization_config

        model = AutoModelForCausalLM.from_pretrained(
            MODEL_ID,
            **load_kwargs
        )

        pipe = pipeline(
            "text-generation",
            model=model,
            tokenizer=tokenizer,
            max_new_tokens=2048,
            temperature=0.01,
            do_sample=True,
            return_full_text=False
        )
        llm_pipeline = HuggingFacePipeline(pipeline=pipe)
        print("LLM loaded successfully.")

    except Exception as e:
        print(f"Error loading LLM: {e}")
        sys.exit(1)


def _extract_json_from_text(text: str) -> dict:
    """
    Extracts JSON from text. 
    Iterates through all potential JSON objects and returns the last valid one 
    (or the one that isn't fully null).
    """
    if not isinstance(text, str):
        return {}
    
    cleaned = text.strip()
    # Remove markdown code blocks
    cleaned = re.sub(r"```json\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"```\s*", "", cleaned)
    
    candidates = []
    
    # Simple brace matching to find all top-level objects
    idx = 0
    while idx < len(cleaned):
        start = cleaned.find("{", idx)
        if start == -1:
            break
            
        balance = 1
        end = -1
        for i in range(start + 1, len(cleaned)):
            if cleaned[i] == "{":
                balance += 1
            elif cleaned[i] == "}":
                balance -= 1
                if balance == 0:
                    end = i
                    break
        
        if end != -1:
            snippet = cleaned[start : end + 1]
            try:
                obj = json.loads(snippet)
                candidates.append(obj)
            except:
                pass
            idx = end + 1
        else:
            break

    if not candidates:
        # Fallback: try finding the last '}' and work backwards? 
        # Or just try regex for strict JSON
        return {"error": "No JSON found", "raw": text[:200]}

    # Heuristic: The result is likely the LAST object, 
    # or the object that has the most non-null headers.
    
    best_candidate = candidates[0]
    
    # Prioritize candidates with 'data' field (CoT format)
    # Return the LAST one, because the first one might be the prompt example echo.
    valid_candidates = [c for c in candidates if "data" in c]
    if valid_candidates:
        return valid_candidates[-1]["data"]

    # Fallback to standard schema candidates
    valid_candidates = []
    for c in candidates:
        header = c.get("header_fields", {})
        if any(v is not None for v in header.values()):
            valid_candidates.append(c)
    
    if valid_candidates:
        return valid_candidates[-1]

    # Last resort: just the last JSON found
    if candidates:
        return candidates[-1]

    return best_candidate

def _apply_regex_fallback(text: str, extracted_data: dict) -> dict:
    """
    Applies regex to find PO numbers and dates if missing.
    """
    if not extracted_data:
        extracted_data = {"header_fields": {}, "line_items": [], "summary": {}}
    
    header = extracted_data.get("header_fields", {})
    if not header:
        header = {}
        extracted_data["header_fields"] = header

    # 1. Fallback for PO Number
    if not header.get("po_number"):
        # Patterns for PO
        po_patterns = [
            r"Order\s*(?:No|#)?\s*[:.]?\s*([A-Z0-9-]{5,})", # Min 5 chars to avoid noise
            r"Ordine\s*(?:N|#)?\s*[:.]?\s*([A-Z0-9-]{3,})",
            r"PO\s*(?:#)?\s*[:.]?\s*([A-Z0-9-]{5,})",
            r"P\.O\.\s*(?:#)?\s*[:.]?\s*([A-Z0-9-]{5,})"
        ]
        for pat in po_patterns:
            match = re.search(pat, text, re.IGNORECASE)
            if match:
                cand = match.group(1)
                header["po_number"] = cand
                break
        
    # 2. Fallback for Dates (order_date) - Look for dates near "Date" keyword?
    if not header.get("order_date"):
        # Simple date pattern dd/mm/yyyy or yyyy-mm-dd
        date_pat = r"\b(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\b"
        dates = re.findall(date_pat, text)
        if dates:
            # Heuristic: the first date is often the order date
            header["order_date"] = dates[0]

    return extracted_data

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", type=str)
    parser.add_argument("--folder", type=str)
    parser.add_argument("--schema", type=str, default="po_schema.json")
    parser.add_argument("--output", type=str, default="results_paddle.jsonl")
    args = parser.parse_args()

    # Load Schema
    current_schema = "{}"
    if os.path.exists(args.schema):
        print(f"Loaded schema from {args.schema}")
        with open(args.schema, 'r', encoding='utf-8') as f:
            current_schema = f.read()
    else:
        print("Warning: Schema file not found. Using empty schema.")

    # Init LLM
    setup_llm()


    # Get files
    files = []
    if args.file:
        files.append(Path(args.file))
    elif args.folder:
        p = Path(args.folder)
        files.extend(list(p.glob(f"*.{ext}") for ext in ["png", "jpg", "jpeg", "pdf"]))
        # Flatten list
        files = [item for sublist in files for item in sublist]
    
    if not files:
        print("No files found.")
        return

    print(f"Processing {len(files)} files...")
    
    results = []
    for fpath in files:
        print(f"Extracting {fpath.name}...")
        
        # 1. OCR via Subprocess
        try:
            print(f"DEBUG: Running OCR subprocess for {fpath.name}...")
            cmd = [sys.executable, "run_ocr_tool.py", "--file", str(fpath)]
            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
            
            if result.returncode != 0:
                print(f"OCR Subprocess Error: {result.stderr}")
                raw_text = ""
            else:
                raw_text = result.stdout.strip()
                
            print(f"DEBUG: OCR Text Length: {len(raw_text)}")
            if len(raw_text) > 200:
                print(f"DEBUG: OCR Text Preview: {raw_text[:200]}...")
            else:
                print(f"DEBUG: OCR Text: {raw_text}")

        except Exception as e:
            print(f"OCR Execution Error: {e}")
            continue

        if not raw_text:
             results.append({"source_file": fpath.name, "error": "Empty OCR"})
             continue

        # 2. LLM
        if llm_pipeline:
            prompt = PROMPT_TEMPLATE.format(extra_schema=current_schema, content=raw_text)
            try:
                print("DEBUG: Invoking LLM...", flush=True)
                
                # DEBUG: Save prompt to file
                with open("debug_last_prompt.txt", "w", encoding="utf-8") as f:
                    f.write(prompt)

                response_str = llm_pipeline.invoke(prompt)
                
                # DEBUG: Save raw response to file
                with open("debug_last_response.txt", "w", encoding="utf-8") as f:
                    f.write(response_str)

                print(f"DEBUG: LLM Response Preview: {response_str[:200]}...", flush=True)
                extracted_data = _extract_json_from_text(response_str)
            except Exception as e:
                print(f"  -> LLM Error: {e}")
                extracted_data = {"error": str(e)}
        else:
            extracted_data = {"error": "LLM not initialized"}

        # Apply Regex Fallback
        extracted_data = _apply_regex_fallback(raw_text, extracted_data)

        extracted_data["source_file"] = fpath.name
        extracted_data["ocr_engine"] = "paddleocr"
        results.append(extracted_data)

    # Save
    with open(args.output, "w", encoding="utf-8") as f:
        for res in results:
            f.write(json.dumps(res, ensure_ascii=False) + "\n")
    
    print(f"Done! Saved to {args.output}")
    if results:
        print("Sample result:")
        print(json.dumps(results[0], indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
