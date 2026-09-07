import glob
import re
import os
os.environ['DISABLE_MODEL_SOURCE_CHECK'] = 'True'


count = 0
for p in glob.glob('*.py'):
    if p == "update_7b.py":
        continue
        
    with open(p, 'r', encoding='utf-8') as f:
        content = f.read()
        
    original = content
        
    if 'po_extraction' in p or 'download' in p or 'diagnose' in p or 'smart' in p:
        content = content.replace('Qwen/Qwen2.5-3B-Instruct', 'Qwen/Qwen2.5-7B-Instruct')
        content = content.replace('Qwen/Qwen2.5-14B-Instruct', 'Qwen/Qwen2.5-7B-Instruct')
        
        # CPU binds
        content = content.replace('DEVICE = "cuda" if torch.cuda.is_available() else "cpu"', 'DEVICE = "cpu"')
        content = content.replace('device = "cuda" if torch.cuda.is_available() else "cpu"', 'device = "cpu"')
        
        # Safely disable bits and bytes
        content = re.sub(r'quantization_config\s*=\s*BitsAndBytesConfig\([^)]+\)', 'quantization_config = None', content, flags=re.DOTALL)
            
        content = content.replace('"device_map": "auto"', '"device_map": "cpu"')
        content = content.replace("'device_map': 'auto'", "'device_map': 'cpu'")
        
        if content != original:
            with open(p, 'w', encoding='utf-8') as f:
                f.write(content)
            print('Patched:', p)
            count += 1

print(f"Total files updated to 7B CPU: {count}")
