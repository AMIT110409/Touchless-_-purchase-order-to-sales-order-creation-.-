"""
PO Extraction Script - Hybrid Approach (Strict Structured Outputs)
---------------------------------------------------------
This script takes the existing OCR text and forces the Qwen 3B model 
to strictly output valid JSON matching a Pydantic schema.
It handles auto-retries if the JSON is broken.

Run: python po_extraction_structured.py --folder test_data
"""

import argparse
import json
import os
os.environ['DISABLE_MODEL_SOURCE_CHECK'] = 'True'

import re
import sys
import subprocess
from pathlib import Path
from typing import List, Optional

import torch
from pydantic import BaseModel, Field, ValidationError
from transformers import AutoTokenizer, AutoModelForCausalLM, pipeline, BitsAndBytesConfig
from langchain_community.llms import HuggingFacePipeline

# ------------------------------------------------
# 1. Pydantic Data Models (The Strict Schema)
# ------------------------------------------------

class HeaderFields(BaseModel):
    customer_id_or_name: Optional[str] = Field(None, description="The entity placing the order (Buyer)")
    vendor_name: Optional[str] = Field(None, description="The entity receiving the order (Supplier)")
    ship_to_address: Optional[str] = Field(None, description="Address where goods are delivered")
    sold_to_address: Optional[str] = Field(None, description="Address being billed")
    requested_delivery_date: Optional[str] = Field(None, description="Requested delivery date (DD/MM/YYYY)")
    po_number: Optional[str] = Field(None, description="The main Purchase Order Number")
    order_date: Optional[str] = Field(None, description="The date the order was placed (DD/MM/YYYY)")

class LineItem(BaseModel):
    material_description: Optional[str] = Field(None, description="Description of the product")
    quantity: Optional[str] = Field(None, description="Number of units ordered (numeric only)")
    unit: Optional[str] = Field(None, description="Unit of measure (e.g., pcs, kg)")
    delivery_date: Optional[str] = Field(None, description="Delivery date specific to this line item")
    material_code: Optional[str] = Field(None, description="Product/Material ID or Code")

class PurchaseOrderDoc(BaseModel):
    header_fields: HeaderFields
    line_items: List[LineItem] = Field(default_factory=list)
    reasoning: Optional[str] = Field(None, description="Brief explanation of how the Customer and Vendor were identified")

# ------------------------------------------------
# CONFIG
# ------------------------------------------------
MODEL_ID = "Qwen/Qwen2.5-3B-Instruct" 
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

llm_pipeline = None

def setup_llm():
    global llm_pipeline
    print(f"Loading model: {MODEL_ID} on {DEVICE} ...")
    
    try:
        quantization_config = None
        if DEVICE == "cuda":
            # 4-bit allows this to run on 4GB VRAM!
            quantization_config = None

        tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
        
        load_kwargs = {"device_map": "auto"}
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
            temperature=0.01, # Extremely low temperature for strict factual extraction
            do_sample=True,
            return_full_text=False
        )
        llm_pipeline = HuggingFacePipeline(pipeline=pipe)
        print("LLM loaded successfully.")

    except Exception as e:
        print(f"Error loading LLM: {e}")
        sys.exit(1)


def generate_prompt(raw_text: str) -> str:
    """Creates a strict system prompt utilizing the injected JSON Schema."""
    schema_json = json.dumps(PurchaseOrderDoc.model_json_schema(), indent=2)
    
    prompt = f"""You are an elite document extraction AI. Your singular goal is to extract Purchase Order data into a perfectly structured JSON object.

CRITICAL INSTRUCTIONS:
1. You MUST output ONLY valid JSON.
2. The JSON MUST exactly match this JSON Schema:
{schema_json}

RULES:
- "quantity" MUST be a string containing only numbers (e.g. "1500" not "1,500 pcs").
- "po_number" MUST be a short identifying string, do not confuse with VAT IDs.
- Do not add markdown blocks like ```json around the output. Just output the raw {{ ... }} object.

DOCUMENT TEXT TO EXTRACT FROM:
===
{raw_text}
===

OUTPUT JSON ONLY (STARTING WITH {{):"""
    return prompt

def extract_with_retries(raw_text: str, max_retries: int = 2) -> PurchaseOrderDoc:
    """Attempts to extract and parse the JSON, retrying if the LLM output is malformed."""
    prompt = generate_prompt(raw_text)
    
    for attempt in range(max_retries):
        try:
            print(f"  Attempt {attempt + 1}: Generating extraction...", flush=True)
            response_str = llm_pipeline.invoke(prompt)
            
            # Clean up potential markdown blocks
            clean_str = response_str.strip()
            clean_str = re.sub(r"```json\s*", "", clean_str, flags=re.IGNORECASE)
            clean_str = re.sub(r"```\s*", "", clean_str)
            
            # Find the first valid JSON object by tracking braces
            start = clean_str.find("{")
            if start != -1:
                balance = 0
                end = -1
                for i in range(start, len(clean_str)):
                    if clean_str[i] == "{":
                        balance += 1
                    elif clean_str[i] == "}":
                        balance -= 1
                        if balance == 0:
                            end = i
                            break
                if end != -1:
                    clean_str = clean_str[start:end+1]
                
            # Parse JSON
            raw_dict = json.loads(clean_str)
            
            # Validate against Pydantic model mathematically
            validated_doc = PurchaseOrderDoc(**raw_dict)
            return validated_doc
            
        except json.JSONDecodeError as e:
            print(f"  [X] Failed to parse JSON on attempt {attempt + 1}: {e}")
        except ValidationError as e:
            print(f"  [X] JSON did not match required schema on attempt {attempt + 1}: {e}")
        except Exception as e:
            print(f"  [X] Unexpected error on attempt {attempt + 1}: {e}")
            
    # Fallback to empty doc if all retries fail
    print("  [!] All extraction attempts failed. Returning empty schema.")
    return PurchaseOrderDoc(
        header_fields=HeaderFields(),
        line_items=[],
        reasoning="Failed to extract valid JSON after max retries."
    )

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", type=str)
    parser.add_argument("--folder", type=str)
    parser.add_argument("--output", type=str, default="results_structured.jsonl")
    args = parser.parse_args()

    # 1. Init LLM
    setup_llm()

    # 2. Get files
    files = []
    if args.file:
        files.append(Path(args.file))
    elif args.folder:
        p = Path(args.folder)
        files.extend(list(p.glob(f"*.{ext}") for ext in ["png", "jpg", "jpeg", "pdf", "pdff"]))
        files = [item for sublist in files for item in sublist]
    
    if not files:
        print("No files found.")
        return

    print(f"Processing {len(files)} files...")
    results = []
    
    for fpath in files:
        print(f"\nExtracting {fpath.name}...")
        
        # 3. Run OCR (using your existing run_ocr_tool.py)
        try:
            print(f"  Running standard OCR subprocess...")
            cmd = [sys.executable, "run_ocr_tool.py", "--file", str(fpath)]
            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
            raw_text = result.stdout.strip()
        except Exception as e:
            print(f"  OCR Execution Error: {e}")
            continue

        if not raw_text or len(raw_text) < 20:
             print("  [!] Empty or too short OCR text, skipping.")
             continue

        # 4. Extract with Robust Pydantic Structured Outputs
        extracted_model = extract_with_retries(raw_text)
        
        # Convert back to dict for saving
        final_dict = extracted_model.model_dump()
        final_dict["source_file"] = fpath.name
        results.append(final_dict)

    # 5. Save Results
    with open(args.output, "w", encoding="utf-8") as f:
        for res in results:
            f.write(json.dumps(res, ensure_ascii=False) + "\n")
    
    print(f"\nDone! Saved {len(results)} structured documents to {args.output}")

if __name__ == "__main__":
    main()
