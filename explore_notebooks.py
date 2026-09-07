import json
import sys

# Ensure UTF-8 output
sys.stdout.reconfigure(encoding='utf-8')

def print_nb(path):
    print(f"=== {path} ===")
    with open(path, 'r', encoding='utf-8') as f:
        nb = json.load(f)
    for i, cell in enumerate(nb.get('cells', [])):
        if cell.get('cell_type') == 'code':
            source = "".join(cell.get('source', []))
            if any(term in source for term in ['celonis', 'tables', 'components', 'query', '4087badc', 'a2e6a325', '5ba818be']):
                print(f"Cell {i}:")
                print(source)
                print("-" * 40)

print_nb("TouchlessOrderCreation.ipynb")
print_nb("TouchlessOrderCreation (2).ipynb")
