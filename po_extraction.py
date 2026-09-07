"""
PO Extraction Script - Claude API Integration (Legacy Wrapper)
---------------------------------------------------------
Uses Claude API (Haiku) for structured data extraction.
Supports global languages and fallback regex for robustness.

Run: python po_extraction.py --folder test_data
"""

import argparse
import json
import os
import re
import sys
import subprocess
from pathlib import Path
from claude_client import ClaudeAPIClient, get_claude_client

# Load environment variables at the very beginning
from dotenv import load_dotenv
load_dotenv()

# ------------------------------------------------
# CONFIG
# ------------------------------------------------

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "claude").lower()
MODEL_ID = "Qwen/Qwen2.5-3B-Instruct" 
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

# ------------------------------------------------
# LLM Setup
# ------------------------------------------------
claude_client = None

def setup_llm():
    global claude_client
    if LLM_PROVIDER == "claude":
        try:
            claude_client = get_claude_client()
            print("Claude API Client initialized.")
        except Exception as e:
            print(f"Error initializing Claude: {e}")
    else:
        print("Local LLM provider selected (legacy mode). Hardware required.")


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
        return {"error": "No JSON found", "raw": text[:200]}

    best_candidate = candidates[0]
    
    # Prioritize candidates with 'data' field (CoT format)
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
        po_patterns = [
            r"Order\s*(?:No|#)?\s*[:.]?\s*([A-Z0-9-]{5,})", 
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
        
    # 2. Fallback for Dates (order_date)
    if not header.get("order_date"):
        date_pat = r"\b(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\b"
        dates = re.findall(date_pat, text)
        if dates:
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

    # Init LLM
    setup_llm()

    # Get files
    files = []
    if args.file:
        files.append(Path(args.file))
    elif args.folder:
        p = Path(args.folder)
        for ext in ["png", "jpg", "jpeg", "pdf", "txt"]:
            files.extend(list(p.glob(f"*.{ext}")))
    
    if not files:
        print("No files found.")
        return

    print(f"Processing {len(files)} files...")
    
    results = []
    for fpath in files:
        print(f"Extracting {fpath.name}...")
        
        raw_text = ""
        if fpath.suffix.lower() == ".txt":
            try:
                raw_text = fpath.read_text(encoding="utf-8", errors="replace")
                print(f"  [TXT] Read {len(raw_text)} chars.")
            except Exception as e:
                print(f"  [TXT] Read error: {e}")
                continue
        else:
            # 1. OCR via Subprocess
            try:
                cmd = [sys.executable, "run_ocr_tool.py", "--file", str(fpath)]
                result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
                if result.returncode != 0:
                    raw_text = ""
                else:
                    raw_text = result.stdout.strip()
            except Exception as e:
                print(f"OCR Execution Error: {e}")
                continue

        if not raw_text:
             results.append({"source_file": fpath.name, "error": "Empty OCR"})
             continue

        # 2. LLM
        extracted_data = {}
        if LLM_PROVIDER == "claude" and claude_client:
            from po_extraction_enhanced import SYSTEM_PROMPT
            prompt = f"Extract structured data from the following text into JSON format:\n\n{raw_text}"
            try:
                print(f"  [Claude] Generating extraction for {fpath.name}...")
                response_str = claude_client.extract_data(prompt, system_prompt=SYSTEM_PROMPT)
                extracted_data = _extract_json_from_text(response_str)
            except Exception as e:
                print(f"  -> Claude Error: {e}")
                extracted_data = {"error": str(e)}
        else:
            extracted_data = {"error": "LLM not initialized or provider mismatch"}

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

if __name__ == "__main__":
    main()
