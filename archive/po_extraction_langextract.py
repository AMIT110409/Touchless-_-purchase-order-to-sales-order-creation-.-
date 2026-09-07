"""
PO Extraction Script - LangExtract Version (LangChain + Pydantic Schema)
------------------------------------------------------------------------
Uses same PaddleOCR subprocess + same Qwen2.5-3B local model,
but extraction uses LangChain's structured extraction with Pydantic schema
instead of free-form prompt + JSON parsing.

Goal: Compare accuracy vs po_extraction_paddle_langchain.py

Run:
  python po_extraction_langextract.py --folder test_data --output results_langextract.jsonl
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
from pydantic import BaseModel, Field
from transformers import AutoTokenizer, AutoModelForCausalLM, pipeline, BitsAndBytesConfig
from langchain_community.llms import HuggingFacePipeline
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import StrOutputParser

# ------------------------------------------------
# PYDANTIC SCHEMA  (the core of LangExtract)
# ------------------------------------------------

class LineItem(BaseModel):
    """A single line item in a purchase order."""
    material_code: Optional[str] = Field(
        default=None,
        description="Product/material code or article number. NOT a line/item number."
    )
    description: Optional[str] = Field(
        default=None,
        description="Material or product description text."
    )
    quantity: Optional[str] = Field(
        default=None,
        description=(
            "Ordered quantity as a plain number. "
            "DO NOT extract price, unit price, or line number as quantity. "
            "European format: '1.500,00' means 1500, '700,00' means 700."
        )
    )
    unit: Optional[str] = Field(
        default=None,
        description="Unit of measure e.g. KG, PCS, M, BAG, L, ST."
    )
    delivery_date: Optional[str] = Field(
        default=None,
        description="Requested delivery date for this line item."
    )


class PurchaseOrder(BaseModel):
    """Structured data extracted from a Purchase Order document."""
    po_number: Optional[str] = Field(
        default=None,
        description=(
            "Purchase order number. Look for 'Order No', 'PO#', 'Bestellnummer'. "
            "Do NOT confuse with Vendor No, VAT number, or customer codes."
        )
    )
    order_date: Optional[str] = Field(
        default=None,
        description="Date the order was placed. Usually near the PO number."
    )
    requested_delivery_date: Optional[str] = Field(
        default=None,
        description="Requested delivery or due date. Different from order date."
    )
    vendor_name: Optional[str] = Field(
        default=None,
        description=(
            "The vendor/supplier NAME (who receives the order). "
            "Usually at the top where the PO is addressed TO."
        )
    )
    customer_id_or_name: Optional[str] = Field(
        default=None,
        description=(
            "The customer/buyer NAME (who is placing the order). "
            "Usually the company whose logo is on the PO header."
        )
    )
    sold_to_id: Optional[str] = Field(
        default=None,
        description="Sold-to party ID / customer number from the document."
    )
    ship_to_address: Optional[str] = Field(
        default=None,
        description="Delivery address. Look for 'Ship To', 'Delivery Address'."
    )
    sold_to_address: Optional[str] = Field(
        default=None,
        description="Billing address. Look for 'Bill To', 'Sold To'."
    )
    line_items: List[LineItem] = Field(
        default_factory=list,
        description="List of ordered items with material, quantity, and unit."
    )


# ------------------------------------------------
# CONFIG
# ------------------------------------------------

MODEL_ID = "Qwen/Qwen2.5-3B-Instruct"
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

# ------------------------------------------------
# LLM SETUP
# ------------------------------------------------

_llm = None

def setup_llm():
    global _llm
    if _llm is not None:
        return _llm

    print(f"Loading model {MODEL_ID} on {DEVICE}...")

    if DEVICE == "cuda":
        bnb_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_compute_dtype=torch.float16,
            bnb_4bit_use_double_quant=True,
            bnb_4bit_quant_type="nf4"
        )
        model = AutoModelForCausalLM.from_pretrained(
            MODEL_ID,
            quantization_config=bnb_config,
            device_map="auto",
            trust_remote_code=True
        )
    else:
        model = AutoModelForCausalLM.from_pretrained(
            MODEL_ID,
            torch_dtype=torch.float32,
            device_map="cpu",
            trust_remote_code=True
        )

    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID, trust_remote_code=True)

    pipe = pipeline(
        "text-generation",
        model=model,
        tokenizer=tokenizer,
        max_new_tokens=1024,
        temperature=0.0,
        do_sample=False,
        return_full_text=False
            )

    _llm = HuggingFacePipeline(pipeline=pipe)
    print("Model loaded.")
    return _llm


# ------------------------------------------------
# OCR — SAME SUBPROCESS AS ORIGINAL SCRIPT
# ------------------------------------------------

def run_ocr(file_path: Path) -> str:
    """Run PaddleOCR via subprocess and return raw text."""
    script_dir = Path(__file__).parent
    ocr_tool = script_dir / "run_ocr_tool.py"

    try:
        result = subprocess.run(
            [sys.executable, str(ocr_tool), "--file", str(file_path)],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        else:
            print(f"  OCR warning: {result.stderr[:200]}")
            return ""
    except Exception as e:
        print(f"  OCR error: {e}")
        return ""


# ------------------------------------------------
# LANGEXTRACT EXTRACTION
# ------------------------------------------------

def extract_po(raw_text: str, llm) -> dict:
    """
    Use Pydantic schema + structured prompt to extract PO fields.
    Compatible with any local HuggingFace model (no function-calling needed).
    Returns a dict matching the standard output format.
    """
    if not raw_text.strip():
        return {"header_fields": {}, "line_items": []}

    # Get schema as JSON for the prompt
    schema_str = json.dumps(PurchaseOrder.model_json_schema(), indent=2)
    text_snippet = raw_text[:3000]

    prompt = f"""You are a Purchase Order extraction system.
Extract the following fields from the document text and return ONLY valid JSON matching the schema below.

SCHEMA:
{schema_str}

RULES:
- Return ONLY raw JSON. No explanation, no markdown, no ```json blocks.
- "quantity" must be a plain number string (e.g. "700" not "700,00 KG").
- Do NOT put item/line numbers in quantity. Only the ordered amount.
- "vendor_name" = who RECEIVES the order (shipped TO).
- "customer_id_or_name" = who PLACES the order (the buyer, top logo).
- If a field is unknown, use null.

DOCUMENT TEXT:
{text_snippet}

JSON OUTPUT:"""

    try:
        from langchain_core.prompts import PromptTemplate
        from langchain_core.output_parsers import StrOutputParser

        chain = PromptTemplate.from_template("{input}") | llm | StrOutputParser()
        raw_output = chain.invoke({"input": prompt})

        print(f"  LLM raw output (first 200 chars): {raw_output[:200]!r}")

        # Extract JSON from output
        json_match = re.search(r'\{.*\}', raw_output, re.DOTALL)
        if not json_match:
            print("  No JSON found in output.")
            return {"header_fields": {}, "line_items": []}

        json_str = json_match.group(0)
        po = PurchaseOrder.model_validate_json(json_str)

        # Convert to standard output format
        header = {
            "po_number": po.po_number,
            "order_date": po.order_date,
            "requested_delivery_date": po.requested_delivery_date,
            "vendor_name": po.vendor_name,
            "customer_id_or_name": po.customer_id_or_name,
            "sold_to_id": po.sold_to_id,
            "ship_to_address": po.ship_to_address,
            "sold_to_address": po.sold_to_address,
        }

        items = []
        for item in po.line_items:
            items.append({
                "material_code": item.material_code,
                "description": item.description,
                "quantity": item.quantity,
                "unit": item.unit,
                "delivery_date": item.delivery_date,
            })

        return {"header_fields": header, "line_items": items}

    except Exception as e:
        print(f"  LangExtract error: {e}")
        return {"header_fields": {}, "line_items": []}


# ------------------------------------------------
# MAIN
# ------------------------------------------------

def load_existing_ocr(jsonl_path: str) -> dict:
    """Load pre-computed raw_text from results_merged.jsonl keyed by source_file stem."""
    ocr_cache = {}
    path = Path(jsonl_path)
    if not path.exists():
        print(f"Warning: {jsonl_path} not found — will skip files without OCR cache.")
        return ocr_cache
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                src = r.get("source_file", "")
                raw = r.get("raw_text", "")
                if src and raw:
                    # Key by both full name and stem for flexible matching
                    ocr_cache[src] = raw
                    ocr_cache[Path(src).stem] = raw
            except:
                pass
    print(f"Loaded OCR cache: {len(ocr_cache)//2} files with raw_text.")
    return ocr_cache


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--folder", type=str, help="Folder of PDF files to process")
    parser.add_argument("--ocr_cache", type=str, default="results_merged.jsonl",
                        help="JSONL file with pre-computed raw_text (avoids re-running OCR)")
    parser.add_argument("--output", type=str, default="results_langextract.jsonl")
    args = parser.parse_args()

    if not args.folder:
        print("Error: specify --folder")
        sys.exit(1)

    # Collect files
    p = Path(args.folder)
    files = []
    for ext in ["pdf", "png", "jpg", "jpeg", "pdff", "PDF"]:
        files.extend(list(p.glob(f"*.{ext}")))
        files.extend(list(p.glob(f"*.{ext.upper()}")))
    # Deduplicate
    files = list({f.name: f for f in files}.values())
    print(f"Found {len(files)} files to process.")

    # Load OCR cache (pre-computed from results_merged.jsonl)
    ocr_cache = load_existing_ocr(args.ocr_cache)

    # Load LLM once
    llm = setup_llm()

    results = []
    skipped_no_ocr = []

    for i, fpath in enumerate(files):
        print(f"\n[{i+1}/{len(files)}] {fpath.name}")

        # Look up pre-computed OCR text
        raw_text = ocr_cache.get(fpath.name) or ocr_cache.get(fpath.stem) or ""

        if not raw_text:
            print(f"  No OCR cache for this file — skipping.")
            skipped_no_ocr.append(fpath.name)
            continue

        print(f"  OCR text: {len(raw_text)} chars (from cache)")

        # LangExtract extraction (Pydantic schema, same LLM)
        extracted = extract_po(raw_text, llm)

        record = {
            "source_file": fpath.name,
            "header_fields": extracted.get("header_fields", {}),
            "line_items": extracted.get("line_items", []),
            "raw_text": raw_text,
            "extraction_method": "langextract_pydantic"
        }
        results.append(record)

        h = record["header_fields"]
        print(f"  -> PO: {h.get('po_number')} | Items: {len(record['line_items'])} | Vendor: {h.get('vendor_name')}")

    # Save
    with open(args.output, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"\n{'='*50}")
    print(f"Done. Saved {len(results)} records to {args.output}")
    if skipped_no_ocr:
        print(f"Skipped {len(skipped_no_ocr)} files (no OCR cache): {skipped_no_ocr[:5]}...")


if __name__ == "__main__":
    main()

