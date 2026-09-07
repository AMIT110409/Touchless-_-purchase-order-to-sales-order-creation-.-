import os
os.environ['DISABLE_MODEL_SOURCE_CHECK'] = 'True'

import sys
import argparse
import json
import logging
from pathlib import Path

# Disable oneDNN/MKLDNN for PaddleOCR CPU inference
os.environ["FLAGS_use_mkldnn"] = "0"
os.environ["PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT"] = "0"

# OFF GPU to avoid DLL errors
os.environ["CUDA_VISIBLE_DEVICES"] = ""

import paddle
from paddleocr import PaddleOCR

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--folder", type=str, required=True, help="Folder containing images/PDFs")
    parser.add_argument("--output", type=str, default="ocr_batch.json", help="Output JSON file")
    args = parser.parse_args()

    # Force CPU
    try:
        paddle.set_device('cpu')
    except:
        pass
    
    print(f"Initializing PaddleOCR on {paddle.device.get_device()}...", file=sys.stderr)
    
    # Initialize PaddleOCR once
    ocr = PaddleOCR(use_textline_orientation=True, lang='en')
    
    # Get files
    p = Path(args.folder)
    files = list(p.glob("*.[pP][nN][gG]")) + \
            list(p.glob("*.[jJ][pP][gG]")) + \
            list(p.glob("*.[jJ][pP][eE][gG]")) + \
            list(p.glob("*.[pP][dD][fF]"))
    
    print(f"Found {len(files)} files to process in {args.folder}")
    
    results = {}
    
    for i, fpath in enumerate(files):
        print(f"[{i+1}/{len(files)}] Processing {fpath.name}...", file=sys.stderr)
        try:
            # Run OCR
            ocr_result = ocr.ocr(str(fpath))
            
            # Extract text
            lines = []
            if ocr_result and ocr_result[0]:
                if isinstance(ocr_result, list) and len(ocr_result) > 0:
                    first_item = ocr_result[0]
                    # Check for rec_texts (PaddleX style)
                    if hasattr(first_item, '__getitem__') and 'rec_texts' in first_item:
                         lines = first_item['rec_texts']
                    # Standard list of lists
                    elif isinstance(first_item, list):
                         for line in first_item:
                             # line format: [[coords], [text, confidence]]
                             if len(line) >= 2 and len(line[1]) >= 1:
                                 lines.append(line[1][0])
            
            full_text = "\n".join(lines)
            results[fpath.name] = full_text
            
        except Exception as e:
            print(f"Error processing {fpath.name}: {e}", file=sys.stderr)
            results[fpath.name] = ""

    # Save to JSON
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    
    print(f"Batch OCR complete. Saved {len(results)} records to {args.output}")

if __name__ == "__main__":
    main()
