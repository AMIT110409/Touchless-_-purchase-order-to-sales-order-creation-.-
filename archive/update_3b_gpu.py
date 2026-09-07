import glob
import re

count = 0
for p in glob.glob('*.py'):
    if p in ["update_7b.py", "update_3b_gpu.py"]:
        continue
        
    with open(p, 'r', encoding='utf-8') as f:
        content = f.read()
        
    original = content
        
    if 'po_extraction' in p or 'download' in p or 'diagnose' in p or 'smart' in p:
        content = content.replace('Qwen/Qwen2.5-7B-Instruct', 'Qwen/Qwen2.5-3B-Instruct')
        content = content.replace('Qwen/Qwen2.5-14B-Instruct', 'Qwen/Qwen2.5-3B-Instruct')
        
        # GPU binds
        content = content.replace('DEVICE = "cpu"', 'DEVICE = "cuda" if torch.cuda.is_available() else "cpu"')
        content = content.replace('device = "cpu"', 'device = "cuda" if torch.cuda.is_available() else "cpu"')
        
        # Re-enable bits and bytes
        bnb_block = """quantization_config = None
        if DEVICE == "cuda":
            quantization_config = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_compute_dtype=torch.float16,
                bnb_4bit_use_double_quant=True,
                bnb_4bit_quant_type="nf4"
            )"""
            
        # Only replace if the block isn't already there
        if 'BitsAndBytesConfig' not in content and 'quantization_config = None' in content:
            content = content.replace('quantization_config = None', bnb_block)
            
        content = content.replace('"device_map": "cpu"', '"device_map": "auto"')
        content = content.replace("'device_map': 'cpu'", "'device_map': 'auto'")
        
        if content != original:
            with open(p, 'w', encoding='utf-8') as f:
                f.write(content)
            print('Patched:', p)
            count += 1

print(f"Total files updated to 3B GPU: {count}")
