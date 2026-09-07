# PO Extraction Script - Setup & Usage Guide

## ✅ System Status

### Hardware & Software
- **GPU**: NVIDIA GeForce RTX 3050 Laptop GPU (4GB VRAM)
- **CUDA**: Version 12.1 ✓ Available
- **PyTorch**: 2.5.1+cu121 ✓ CUDA Enabled
- **Python**: 3.10 (in virtual environment)

### Dependencies Status
✅ All required packages installed:
- `paddleocr` - OCR engine
- `paddlex[ocr]` - Pipeline dependencies
- `langchain` & `langchain-community` - LLM framework
- `transformers` - Hugging Face models
- `torch` (CUDA 12.1) - Deep learning framework
- `sentence-transformers` - Embeddings
- `accelerate` & `bitsandbytes` - Model optimization

### Models Downloaded
✅ All PaddleOCR models cached in: `C:\Users\Abcom\.paddlex\official_models\`
- PP-StructureV3 (layout analysis)
- PP-OCRv5 (text detection & recognition)
- PP-DocLayout (document layout)
- Table & formula detection models

---

## 📋 What You Need to Run the Script

### 1. Input Files
You need **Purchase Order documents** in one of these formats:
- PDF files (`.pdf`)
- PNG images (`.png`)
- JPG/JPEG images (`.jpg`, `.jpeg`)

**Current test data**: You have 5 PNG images in `test_data\` folder

### 2. Schema File (Optional)
- **File**: `po_schema.json` ✓ Already created
- **Purpose**: Defines which fields to extract from POs

**Your current schema extracts**:
- **Header**: customer_id_or_name, requested_delivery_date, po_number, order_date
- **Line Items**: material_description, quantity, delivery_date, material_code
- **Summary**: detected_language, confidence_score

### 3. Virtual Environment
✅ Already activated at: `.\venv\`

---

## 🚀 How to Run the Script

### Basic Usage

#### Option 1: Process a Single File
```powershell
python po_extraction_paddle_langchain.py --file "path\to\your\po.pdf" --schema "po_schema.json"
```

#### Option 2: Process Multiple Files in a Folder
```powershell
python po_extraction_paddle_langchain.py --folder "test_data" --schema "po_schema.json" --output "results.jsonl"
```

#### Option 3: Process Without Custom Schema (Uses Default)
```powershell
python po_extraction_paddle_langchain.py --file "path\to\your\po.pdf"
```

### Command Line Arguments

| Argument | Required | Description | Example |
|----------|----------|-------------|---------|
| `--file` | Yes* | Path to single PDF/image file | `--file "invoice.pdf"` |
| `--folder` | Yes* | Path to folder with multiple files | `--folder "test_data"` |
| `--schema` | No | Path to custom JSON schema | `--schema "po_schema.json"` |
| `--output` | No | Output file name (default: results.jsonl) | `--output "my_results.jsonl"` |

*Either `--file` OR `--folder` is required (not both)

---

## 📝 Example Commands

### Test with Your Current Data
```powershell
# Process all 5 PNG images in test_data folder
python po_extraction_paddle_langchain.py --folder "test_data" --schema "po_schema.json" --output "test_results.jsonl"
```

### Process a Single Test Image
```powershell
# Process just one image
python po_extraction_paddle_langchain.py --file "test_data\uploaded_image_0_1768204557338.png" --schema "po_schema.json"
```

---

## 📤 Output Format

Results are saved as **JSONL** (JSON Lines) format:
- One JSON object per line
- Each line represents one processed document

**Example output structure**:
```json
{
  "header_fields": {
    "customer_id_or_name": "ABC Corp",
    "requested_delivery_date": "2024-01-15",
    "po_number": "PO-12345",
    "order_date": "2024-01-10"
  },
  "line_items": [
    {
      "material_description": "Steel Rods",
      "quantity": 100,
      "delivery_date": "2024-01-15",
      "material_code": "SR-001"
    }
  ],
  "summary": {
    "detected_language": "English",
    "confidence_score": 0.95
  },
  "source_file": "uploaded_image_0_1768204557338.png"
}
```

---

## ⚡ Performance Notes

### First Run
- **LLM Model Download**: Qwen2.5-3B-Instruct (~6-8 GB) will download on first run
- **Estimated Time**: 5-15 minutes for model download
- **After Download**: Models are cached, subsequent runs are much faster

### Processing Speed (Estimated)
- **With GPU (CUDA)**: ~30-60 seconds per document
- **CPU Only**: ~2-5 minutes per document

### Memory Requirements
- **GPU VRAM**: ~3-4 GB (you have 4 GB - should work)
- **System RAM**: ~8 GB recommended

---

## 🔧 Troubleshooting

### If Script Fails to Start
1. Ensure virtual environment is activated:
   ```powershell
   .\venv\Scripts\Activate.ps1
   ```

2. Verify CUDA is available:
   ```powershell
   python -c "import torch; print('CUDA:', torch.cuda.is_available())"
   ```

### If Out of GPU Memory
The script will automatically fall back to CPU if GPU runs out of memory.

To force CPU mode, edit line 35 in `po_extraction_paddle_langchain.py`:
```python
DEVICE = "cpu"  # Force CPU mode
```

### If Model Download Fails
Run the download script separately:
```powershell
python download_models.py
```

---

## 📁 File Structure

```
extraction ocr scripit/
├── po_extraction_paddle_langchain.py  # Main script
├── po_schema.json                     # Your custom schema
├── download_models.py                 # Pre-download models
├── requirements.txt                   # Dependencies
├── test_data/                         # Your test images (5 PNGs)
├── venv/                              # Virtual environment
└── results.jsonl                      # Output (created after run)
```

---

## ✨ Ready to Run!

Everything is set up and ready. To start extracting:

```powershell
# Quick test with your existing data
python po_extraction_paddle_langchain.py --folder "test_data" --schema "po_schema.json"
```

The script will:
1. ✅ Load PaddleOCR models (already cached)
2. ✅ Load Qwen2.5-3B LLM (will download on first run)
3. ✅ Process all images in test_data folder
4. ✅ Extract data according to po_schema.json
5. ✅ Save results to results.jsonl

**Note**: First run will take longer due to LLM model download (~6-8 GB).
