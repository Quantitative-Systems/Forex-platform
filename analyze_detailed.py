import json
import re

with open('research/style_matrix.py') as f:
    content = f.read()

# Look at the 4 spread positions
print("=== Spread positions ===")
for m in re.finditer(r'spread', content, re.IGNORECASE):
    start = max(0, m.start()-30)
    end = min(len(content), m.end()+30)
    print(f"Position {m.start()}: ...{content[start:end]}...")

print("\n=== Equity tracking ===")
for m in re.finditer(r'equity', content, re.IGNORECASE):
    start = max(0, m.start()-30)
    end = min(len(content), m.end()+30)
    print(f"Position {m.start()}: ...{content[start:end]}...")

print("\n=== R-multiples tracking (rs list) ===")
# Find the rs list initialization and usage
idx = content.find('rs: list')
if idx >= 0:
    print(f"rs: list found at {idx}")
    print(content[idx:idx+500])

print("\n=== Position tracking ===")
idx = content.find('position = None')
if idx >= 0:
    print(f"position = None found at {idx}")
    print(content[idx:idx+500])