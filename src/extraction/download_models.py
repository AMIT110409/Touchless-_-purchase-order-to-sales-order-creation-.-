"""
Download PaddleOCR and LLM models locally
Run this script before running the main extraction script to pre-download all models
"""

import os
os.environ['DISABLE_MODEL_SOURCE_CHECK'] = 'True'

from pathlib import Path

print("=" * 60)
print("Step 1: Downloading PaddleOCR Models")
print("=" * 60)

try:
    from paddleocr import PPStructureV3
    print("Initializing PP-StructureV3 (this will download models)...")
    structure_engine = PPStructureV3(lang="en")
    print("✓ PaddleOCR models downloaded successfully!")
except Exception as e:
    print(f"✗ Error downloading PaddleOCR models: {e}")

print("\n" + "=" * 60)
print("Step 2: Downloading LLM Model (Qwen2.5-3B-Instruct)")
print("=" * 60)
print("This may take 5-15 minutes depending on your internet speed...")

try:
    from transformers import AutoTokenizer, AutoModelForCausalLM
    
    MODEL_ID = "Qwen/Qwen2.5-3B-Instruct"
    
    print(f"Downloading tokenizer for {MODEL_ID}...")
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
    print("✓ Tokenizer downloaded!")
    
    print(f"Downloading model {MODEL_ID} (~6-8 GB)...")
    model = AutoModelForCausalLM.from_pretrained(
        MODEL_ID,
        torch_dtype="auto",
        device_map="auto",
        trust_remote_code=True
    )
    print("✓ LLM model downloaded successfully!")
    
    # Show cache location
    from transformers import TRANSFORMERS_CACHE
    cache_dir = os.environ.get('HF_HOME', os.environ.get('TRANSFORMERS_CACHE', Path.home() / '.cache' / 'huggingface'))
    print(f"\nModels cached at: {cache_dir}")
    
except Exception as e:
    print(f"✗ Error downloading LLM model: {e}")

print("\n" + "=" * 60)
print("All models downloaded! You can now run the main script.")
print("=" * 60)
