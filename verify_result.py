import json

with open('research/results/style_matrix.json') as f:
    d = json.load(f)

passes = [r for r in d if r['verdict'] == 'PASS']
print(f'Total cells: {len(d)}')
print(f'PASS cells: {len(passes)}')
for r in passes:
    print(f"{r['pair']:7s} {r['set_key']} x {r['style']:11s} trades={r['trades']} exp={r['expectancy_r']:.3f}R pf={r['profit_factor']:.2f}")