import json
import re

with open('research/style_matrix.py') as f:
    content = f.read()

# Print the full run_cell function
idx = content.find('def run_cell')
print("=== FULL run_cell function ===")
print(content[idx:])

# Also check the CellResult definition
print("\n\n=== Looking for CellResult ===")
for m in re.finditer(r'CellResult', content):
    start = max(0, m.start()-50)
    end = min(len(content), m.end()+100)
    print(content[start:end])