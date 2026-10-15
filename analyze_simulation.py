import json
import re

# Read the simulation code
with open('research/style_matrix.py') as f:
    content = f.read()

print("=== Looking for cost/spread/slip modeling in style_matrix.py ===")
for term in ['spread', 'commission', 'slippage', 'cost', 'pip', 'fee']:
    indices = [m.start() for m in re.finditer(term, content, re.IGNORECASE)]
    print(f"'{term}': found at {len(indices)} positions")

# Look at the run_cell function
idx = content.find('def run_cell')
print("\n=== run_cell function - first 600 chars ===")
print(content[idx:idx+600])

# Check for equity tracking and R calculation
print("\n=== Looking for equity/peak/max_dd tracking ===")
for term in ['equity', 'peak', 'max_dd', 'rs', 'R-multiple', 'realized']:
    indices = [m.start() for m in re.finditer(term, content, re.IGNORECASE)]
    print(f"'{term}': found at {len(indices)} positions")

# Check the deterministic seed / random usage
print("\n=== Looking for random or seed ===")
indices = [m.start() for m in re.finditer(r'\b(seed|random)\b', content, re.IGNORECASE)]
pprint = [content[max(0,i-5):i+30] for i in indices[:5]]
print(f"'seed/random': found at {len(indices)} positions: {pprint}")