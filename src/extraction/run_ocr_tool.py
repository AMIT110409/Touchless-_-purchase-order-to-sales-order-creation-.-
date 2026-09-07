import sys
import argparse
import os
os.environ['DISABLE_MODEL_SOURCE_CHECK'] = 'True'
os.environ['PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK'] = 'True'
os.environ["FLAGS_use_mkldnn"] = "0"
os.environ["PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT"] = "0"

import io

# Fix stdout encoding for Windows (handles Unicode chars like Greek letters, Chinese, etc.)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# OFF GPU
os.environ["CUDA_VISIBLE_DEVICES"] = ""
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"

# -- FIX FOR PADDLEX DEPRECATED LANGCHAIN IMPORTS --
import types
class DummyMock: pass
langchain_mock = types.ModuleType('langchain')
sys.modules['langchain'] = langchain_mock

docstore_mock = types.ModuleType('langchain.docstore')
docstore_mock.document = types.ModuleType('langchain.docstore.document')
docstore_mock.document.Document = DummyMock
sys.modules['langchain.docstore'] = docstore_mock
sys.modules['langchain.docstore.document'] = docstore_mock.document

ts_mock = types.ModuleType('langchain.text_splitter')
ts_mock.RecursiveCharacterTextSplitter = DummyMock
sys.modules['langchain.text_splitter'] = ts_mock
# --------------------------------------------------

from paddleocr import PaddleOCR
import paddle

def main():
    # Force CPU
    try:
        paddle.set_device('cpu')
    except:
        pass
        
    print(f"DEBUG: Paddle Device: {paddle.device.get_device()}", file=sys.stderr)
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", type=str, required=True)
    args = parser.parse_args()
    
    # Update extraction for PaddleX result structure
    import os
    os.environ["DISABLE_MODEL_SOURCE_CHECK"] = "True"
    
    # Initialize PaddleOCR - set parameters compatible with version 3.3.2
    ocr = PaddleOCR(use_textline_orientation=True, lang='en')
    
    result = ocr.ocr(args.file)
    print(f"DEBUG: Result Type: {type(result)}", file=sys.stderr)
    if not result:
        return

    # Process all pages in the document
    lines = []
    try:
        if isinstance(result, list) and len(result) > 0:
            for page_result in result:
                if page_result is None:
                    continue
                # Check for rec_texts in PaddleX page dictionary structure
                if hasattr(page_result, '__getitem__') and 'rec_texts' in page_result:
                     lines.extend(page_result['rec_texts'])
                # Fallback for list-of-lists structure (regular PaddleOCR format)
                elif isinstance(page_result, list):
                     for line in page_result:
                         if isinstance(line, list) and len(line) > 1 and isinstance(line[1], (list, tuple)) and len(line[1]) > 0:
                             lines.append(line[1][0])
    except Exception as e:
        print(f"DEBUG: Parsing Error: {e}", file=sys.stderr)
    
    # Print text to stdout (utf-8 encoded)
    print("\n".join(lines))

if __name__ == "__main__":
    main()
