import json

with open('research/results/style_matrix_macro.json') as f:
    d = json.load(f)

print("=== Macro deep-history non-NO_DATA cells ===")
for r in d:
    if r['trades'] > 0 or r['verdict'] != 'NO_DATA':
        print(f"{r['pair']:7s} {r['set_key']} x {r['style']:11s} trades={r['trades']} exp={r['expectancy_r']:.3f}R pf={r['profit_factor']:.2f} verdict={r['verdict']}")

print(f"\nTotal cells: {len(d)}")
counts = {}
for r in d:
    key = f"{r['pair']} {r['set_key']}"
    counts[key] = counts.get(key, 0) + 1
print("Per-set counts:")
for k, v in counts.items():
    print(f"  {k}: {v}")