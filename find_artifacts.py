import os

results = []
for root, dirs, files in os.walk('.'):
    # Skip .git
    if '.git' in root:
        dirs[:] = [d for d in dirs if d != '.git']
    for f in files:
        if f.startswith(('snapshot', 'provenance', 'audit')) and not f.endswith('.py'):
            path = os.path.join(root, f)
            results.append(path)
for r in results:
    print(r)
print(f"Total: {len(results)}")