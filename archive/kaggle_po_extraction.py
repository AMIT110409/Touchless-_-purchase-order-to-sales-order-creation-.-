"""
=============================================================
  PO Extraction — Kaggle/Colab Edition (Qwen2.5-14B)
=============================================================
Run this script on Kaggle (with T4 GPU) or Google Colab.

USAGE:
  1. Upload your PO PDF/TXT files to a folder (e.g. /kaggle/input/po-files/)
  2. Run:  python kaggle_po_extraction.py --folder /kaggle/input/po-files/ --output results.jsonl
  3. Download the results.jsonl and run local mapping on your machine.

For Kaggle Notebook, paste the cells below or run this as a script.
"""

# --------------------------------------------------
# Cell 1: Install Dependencies
# --------------------------------------------------
# !pip install -q torch transformers accelerate bitsandbytes PyMuPDF json-repair surya-ocr

import argparse
import json
import os
os.environ['DISABLE_MODEL_SOURCE_CHECK'] = 'True'

import re
import sys
from pathlib import Path

# --------------------------------------------------
# Cell 2: Configuration
# --------------------------------------------------

MODEL_ID = "Qwen/Qwen2.5-3B-Instruct"   # 14B model — fits in T4 16GB with 4-bit
FALLBACK_MODEL_ID = "Qwen/Qwen2.5-3B-Instruct"  # Fallback if 14B OOMs

# --------------------------------------------------
# Cell 3: Extraction Prompt (Production-Grade)
# --------------------------------------------------

PROMPT_TEMPLATE = """You are a specialized Purchase Order extraction system. Extract structured data from the document below into JSON.

**RULES**:
- Output ONLY a single raw JSON object. No markdown, no explanation.
- Dates: DD/MM/YYYY format.
- If a value is not found, use null. NEVER output "不明", "未提供", "unknown", or "N/A".
- quantity must be a plain number (no units). Convert European format: 1.500 = 1500, 24.000 = 24000.
- material_code is an alphanumeric identifier, NEVER a weight like "25Kg" or price like "70620 EUR".

**LANGUAGE FIELD MAPPING**:
| Field | EN | DE | FR | JP | ZH |
|-------|----|----|----|----|-----|
| PO Number | PO No / Order No | Bestellnummer / Nr. | N° de commande | 注文番号 / 発注番号 | 订单号 / 采购订单号 |
| Order Date | Date | Bestelldatum | Date commande | 発注日 / 注文日 | 订单日期 |
| Delivery Date | Delivery Date | Lieferdatum / Liefertermin | Date de livraison | 納期 / 希望納期 | 交货日期 |
| Customer | Customer / Buyer | Kunde / Auftraggeber | Client | 買主 / 購入者 | 客户 / 买方 |
| Material Code | Part No / Item No | Artikelnr. / Material-Nr. | Réf. article | 品番 / 部品番号 | 物料编号 / 料号 |
| Material Desc | Description | Artikelbezeichnung / Bezeichnung | Désignation | 品名 / 品目 | 物料描述 / 品名 |
| Quantity | Qty | Menge | Quantité | 数量 | 数量 |
| Unit | UoM | Einheit / ME | Unité | 単位 | 单位 |

**CONTEXT**: Envalior B.V. (or DSM / Envalior) is ALWAYS the vendor/supplier. The OTHER company is the customer/buyer.

**JSON SCHEMA**:
{{"header_fields": {{"po_number": "...", "order_date": "DD/MM/YYYY", "requested_delivery_date": "DD/MM/YYYY", "customer_id_or_name": "...", "vendor_name": "...", "ship_to_address": "...", "sold_to_address": "..."}}, "line_items": [{{"material_description": "...", "quantity": "...", "unit": "...", "delivery_date": "DD/MM/YYYY", "material_code": "..."}}]}}

**DOCUMENT TEXT**:
{content}

JSON:"""


# --------------------------------------------------
# Cell 4: Text Extraction (PyMuPDF Native + Surya Fallback)
# --------------------------------------------------

def extract_text_pymupdf(pdf_path: str) -> str:
    """Extract text using PyMuPDF native extraction (works for digital PDFs)."""
    import fitz
    try:
        doc = fitz.open(pdf_path)
        all_text = []
        for page in doc:
            text = page.get_text("text")
            if text.strip():
                all_text.append(text.strip())
        doc.close()
        return "\n\n".join(all_text)
    except Exception as e:
        print(f"  [PyMuPDF] Error: {e}")
        return ""


def extract_text_surya(pdf_path: str) -> str:
    """Fallback: use Surya OCR for scanned documents (multilingual)."""
    try:
        from surya.ocr import run_ocr
        from surya.model.detection.model import load_model as load_det_model
        from surya.model.detection.processor import load_processor as load_det_processor
        from surya.model.recognition.model import load_model as load_rec_model
        from surya.model.recognition.processor import load_processor as load_rec_processor
        from PIL import Image
        import fitz

        # Convert PDF pages to images
        doc = fitz.open(pdf_path)
        images = []
        for page in doc:
            pix = page.get_pixmap(dpi=200)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            images.append(img)
        doc.close()

        if not images:
            return ""

        # Load Surya models (cached after first call)
        det_model = load_det_model()
        det_processor = load_det_processor()
        rec_model = load_rec_model()
        rec_processor = load_rec_processor()

        # Run OCR — auto-detects language
        langs = [["en", "de", "fr", "ja", "zh"]] * len(images)
        results = run_ocr(images, langs, det_model, det_processor, rec_model, rec_processor)

        all_text = []
        for page_result in results:
            lines = [line.text for line in page_result.text_lines]
            all_text.append("\n".join(lines))

        return "\n\n".join(all_text)

    except ImportError:
        print("  [Surya] Not installed. Run: pip install surya-ocr")
        return ""
    except Exception as e:
        print(f"  [Surya] Error: {e}")
        return ""


def extract_text(file_path: str) -> tuple[str, str]:
    """
    Smart text extraction:
    1. If .txt file: read directly
    2. If PDF: try PyMuPDF native text first
    3. If native text is too short: fall back to Surya OCR
    Returns: (text, engine_used)
    """
    path = Path(file_path)

    # Direct text files (email bodies)
    if path.suffix.lower() == ".txt":
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            return text.strip(), "direct_text"
        except Exception:
            return "", "error"

    # PDFs: try native text first
    if path.suffix.lower() == ".pdf":
        native_text = extract_text_pymupdf(str(path))
        if len(native_text) > 100:
            print(f"  [PyMuPDF] Got {len(native_text)} chars of native text ✓")
            return native_text, "pymupdf_native"

        # Fallback: Surya OCR for scanned PDFs
        print(f"  [PyMuPDF] Only {len(native_text)} chars — falling back to Surya OCR...")
        surya_text = extract_text_surya(str(path))
        if surya_text:
            print(f"  [Surya] Got {len(surya_text)} chars ✓")
            return surya_text, "surya_ocr"

        # Last resort: return whatever PyMuPDF got
        return native_text, "pymupdf_sparse"

    # Images: Surya OCR
    if path.suffix.lower() in (".png", ".jpg", ".jpeg", ".tiff", ".bmp"):
        try:
            from surya.ocr import run_ocr
            from surya.model.detection.model import load_model as load_det_model
            from surya.model.detection.processor import load_processor as load_det_processor
            from surya.model.recognition.model import load_model as load_rec_model
            from surya.model.recognition.processor import load_processor as load_rec_processor
            from PIL import Image

            img = Image.open(str(path)).convert("RGB")
            det_model = load_det_model()
            det_processor = load_det_processor()
            rec_model = load_rec_model()
            rec_processor = load_rec_processor()

            langs = [["en", "de", "fr", "ja", "zh"]]
            results = run_ocr([img], langs, det_model, det_processor, rec_model, rec_processor)
            text = "\n".join([line.text for line in results[0].text_lines])
            return text, "surya_ocr"
        except Exception as e:
            print(f"  [Surya Image] Error: {e}")
            return "", "error"

    return "", "unsupported"


# --------------------------------------------------
# Cell 5: LLM Setup (Qwen 14B, 4-bit quantized)
# --------------------------------------------------

_llm_pipeline = None

def setup_llm():
    """Load the LLM with 4-bit quantization for GPU efficiency."""
    global _llm_pipeline
    import torch
    from transformers import AutoTokenizer, AutoModelForCausalLM, pipeline, BitsAndBytesConfig

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model_id = MODEL_ID

    print(f"Loading model: {model_id} on {device}...")

    try:
        quantization_config = None

        tokenizer = AutoTokenizer.from_pretrained(model_id)
        model = AutoModelForCausalLM.from_pretrained(
            model_id,
            device_map="auto",
            quantization_config=quantization_config
            )

        pipe = pipeline(
            "text-generation",
            model=model,
            tokenizer=tokenizer,
            max_new_tokens=4096,
            do_sample=False,
            return_full_text=False
            )
        _llm_pipeline = pipe
        print(f"✅ Model loaded successfully on {device}")
        return True

    except Exception as e:
        if "14B" in model_id:
            print(f"⚠️ 14B model failed ({e}). Trying 7B fallback...")
            model_id = FALLBACK_MODEL_ID
            try:
                tokenizer = AutoTokenizer.from_pretrained(model_id)
                model = AutoModelForCausalLM.from_pretrained(
                    model_id,
                    device_map="auto",
                    quantization_config=quantization_config
            )
                pipe = pipeline(
                    "text-generation",
                    model=model,
                    tokenizer=tokenizer,
                    max_new_tokens=4096,
                    do_sample=False,
                    return_full_text=False
            )
                _llm_pipeline = pipe
                print(f"✅ Fallback model {model_id} loaded successfully")
                return True
            except Exception as e2:
                print(f"❌ Both models failed: {e2}")
                return False
        else:
            print(f"❌ Model load failed: {e}")
            return False


# --------------------------------------------------
# Cell 6: JSON Extraction with Repair + Retry
# --------------------------------------------------

def extract_json_robust(raw_text: str) -> dict:
    """
    Extract JSON from LLM output with multi-layer repair:
    1. Try direct parse
    2. Try json_repair library
    3. Try regex-based extraction
    """
    if not isinstance(raw_text, str) or not raw_text.strip():
        return {"error": "Empty LLM output"}

    cleaned = raw_text.strip()
    cleaned = re.sub(r"```json\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"```\s*", "", cleaned)

    # -- Attempt 1: Find balanced braces and parse --
    start = cleaned.find("{")
    if start == -1:
        return {"error": "No JSON found", "raw": raw_text[:300]}

    balance = 0
    end = -1
    for i in range(start, len(cleaned)):
        if cleaned[i] == "{":
            balance += 1
        elif cleaned[i] == "}":
            balance -= 1
            if balance == 0:
                end = i
                break

    if end == -1:
        snippet = cleaned[start:]
    else:
        snippet = cleaned[start:end + 1]

    # Try direct parse
    try:
        obj = json.loads(snippet)
        if "data" in obj and isinstance(obj["data"], dict):
            return obj["data"]
        return obj
    except json.JSONDecodeError:
        pass

    # -- Attempt 2: json_repair library --
    try:
        from json_repair import repair_json
        repaired = repair_json(snippet, return_objects=True)
        if isinstance(repaired, dict):
            if "data" in repaired and isinstance(repaired["data"], dict):
                return repaired["data"]
            return repaired
    except ImportError:
        print("  [Warning] json_repair not installed. Run: pip install json-repair")
    except Exception:
        pass

    # -- Attempt 3: Manual fixes --
    try:
        # Common LLM JSON errors: trailing commas, single quotes
        fixed = snippet
        fixed = re.sub(r",\s*}", "}", fixed)        # trailing comma
        fixed = re.sub(r",\s*]", "]", fixed)         # trailing comma in arrays
        fixed = fixed.replace("'", '"')               # single quotes to double
        obj = json.loads(fixed)
        if "data" in obj and isinstance(obj["data"], dict):
            return obj["data"]
        return obj
    except json.JSONDecodeError as e:
        return {"error": f"JSON parse error: {e}", "raw": snippet[:300]}


def call_llm(text_content: str) -> dict:
    """Call the LLM with the prompt and parse the response."""
    if _llm_pipeline is None:
        return {"error": "LLM not loaded"}

    # Truncate very long text to fit context window
    max_chars = 8000
    if len(text_content) > max_chars:
        text_content = text_content[:max_chars] + "\n... [TRUNCATED]"

    prompt = PROMPT_TEMPLATE.format(content=text_content)

    try:
        result = _llm_pipeline(prompt)
        raw_output = result[0]["generated_text"]
        return extract_json_robust(raw_output)
    except Exception as e:
        return {"error": f"LLM inference error: {e}"}


# --------------------------------------------------
# Cell 7: Post-Processing & Validation
# --------------------------------------------------

def _clean_unknown_values(obj):
    """Replace Japanese/Chinese 'unknown' placeholders with null."""
    unknown_markers = {"不明", "未提供", "unknown", "N/A", "n/a", "NA", "なし", "无", "未知"}
    if isinstance(obj, dict):
        return {k: _clean_unknown_values(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_clean_unknown_values(item) for item in obj]
    elif isinstance(obj, str) and obj.strip() in unknown_markers:
        return None
    return obj


def _is_weight_as_material_code(value: str) -> bool:
    """Detect if material_code was wrongly filled with quantity/weight/price."""
    if not value or not isinstance(value, str):
        return False
    s = value.strip()
    if re.match(r"^\d+([.,]\d+)?\s*(kg|KG|pcs|pc|ton|lb|g|eur|€|YEN|yen)\s*$", s, re.IGNORECASE):
        return True
    return False


def post_process(result: dict, source_file: str) -> dict:
    """Clean and validate extracted data."""
    # Remove unknown placeholders
    result = _clean_unknown_values(result)

    # Fix line items
    if "line_items" in result:
        cleaned_items = []
        for item in result["line_items"]:
            if not isinstance(item, dict):
                continue
            # Fix material_code if it's actually a weight
            mc = item.get("material_code", "")
            if mc and _is_weight_as_material_code(mc):
                item["material_code"] = None
            cleaned_items.append(item)
        result["line_items"] = cleaned_items

    # Try to extract PO number from filename if missing
    header = result.get("header_fields", {})
    if not header.get("po_number"):
        # Try filename patterns like "1014374731.pdf" or "PO1014374731_..."
        m = re.search(r"(?:PO)?(\d{8,13})", source_file)
        if m:
            header["po_number"] = m.group(1)

    result["source_file"] = source_file
    return result


# --------------------------------------------------
# Cell 8: Main Pipeline
# --------------------------------------------------

def process_folder(input_folder: str, output_file: str):
    """Process all PDF/TXT files in a folder and write results to JSONL."""
    folder = Path(input_folder)
    if not folder.exists():
        print(f"❌ Folder not found: {folder}")
        sys.exit(1)

    # Collect supported files
    supported_ext = {".pdf", ".txt", ".png", ".jpg", ".jpeg"}
    files = sorted([
        f for f in folder.iterdir()
        if f.suffix.lower() in supported_ext
        and not f.name.startswith(".")
    ])

    # Skip tiny images (logos)
    files = [f for f in files if not (
        f.suffix.lower() in {".png", ".jpg", ".jpeg"} and f.stat().st_size < 80000
    )]

    print(f"\n{'='*60}")
    print(f"  Processing {len(files)} files from {folder}")
    print(f"  Output: {output_file}")
    print(f"  Model: {MODEL_ID}")
    print(f"{'='*60}\n")

    # Setup LLM
    if not setup_llm():
        print("❌ Failed to load LLM. Exiting.")
        sys.exit(1)

    results = []
    success = 0
    errors = 0

    with open(output_file, "w", encoding="utf-8") as out_f:
        for i, file_path in enumerate(files, 1):
            print(f"\n-- [{i}/{len(files)}] Extracting: {file_path.name} --")

            # Step 1: Extract text
            text_content, engine = extract_text(str(file_path))
            if not text_content or len(text_content.strip()) < 10:
                print(f"  ⚠️ No usable text extracted. Skipping.")
                error_record = {"source_file": file_path.name, "error": "Empty text", "engine": engine}
                out_f.write(json.dumps(error_record, ensure_ascii=False) + "\n")
                errors += 1
                continue

            print(f"  [Text] {len(text_content)} chars via {engine}")

            # Step 2: LLM extraction
            print(f"  [LLM] Generating extraction...")
            extracted = call_llm(text_content)

            # Check for errors
            if "error" in extracted and "header_fields" not in extracted:
                print(f"  ⚠️ LLM error: {extracted.get('error', '')[:80]}")
                extracted["source_file"] = file_path.name
                extracted["ocr_engine"] = engine
                out_f.write(json.dumps(extracted, ensure_ascii=False) + "\n")
                errors += 1
                continue

            # Step 3: Post-process
            final = post_process(extracted, file_path.name)
            final["ocr_engine"] = engine

            # Summary
            header = final.get("header_fields", {})
            items = final.get("line_items", [])
            po = header.get("po_number", "?")
            cust = header.get("customer_id_or_name", "?")
            print(f"  ✅ PO: {po} | Customer: {cust} | Items: {len(items)}")
            success += 1

            out_f.write(json.dumps(final, ensure_ascii=False) + "\n")

    print(f"\n{'='*60}")
    print(f"  ✅ EXTRACTION COMPLETE")
    print(f"  Success: {success} | Errors: {errors} | Total: {len(files)}")
    print(f"  Output: {output_file}")
    print(f"{'='*60}\n")


# --------------------------------------------------
# Cell 9: Entry Point
# --------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PO Extraction (Kaggle/Colab Edition)")
    parser.add_argument("--folder", type=str, required=True, help="Folder with PDF/TXT files")
    parser.add_argument("--output", type=str, default="results_kaggle.jsonl", help="Output JSONL file")
    args = parser.parse_args()

    process_folder(args.folder, args.output)
