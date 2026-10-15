import json
import sys

# Load the style_matrix results and find GBPUSD SET_5 SCALPING
with open('research/results/style_matrix.json') as f:
    d = json.load(f)

for r in d:
    if r['pair'] == 'GBPUSD' and r['set_key'] == 'SET_5' and r['style'] == 'SCALPING':
        print("=== GBPUSD SET_5 SCALPING cell ===")
        print(json.dumps(r, indent=2))
        
        # Calculate what 6 winners/26 losers would give
        rs = [r['expectancy_r']]  # mean_r, but we need the full distribution
        print(f"\nExpected distribution:")
        print(f"  trades: {r['trades']}")
        print(f"  win_rate: {r['win_rate']}")
        print(f"  mean_r: {r['mean_r']:.6f}")
        print(f"  expectancy_r: {r['expectancy_r']:.6f}R")
        print(f"  profit_factor: {r['profit_factor']:.6f}")
        print(f"  max_dd: {r['max_dd']:.6f}")
        
        # If 6 winners and 26 losers at win_rate = 18.75%
        # Let's see if we can reverse-engineer the R values
        n = r['trades']
        w = int(n * r['win_rate'])
        l = n - w
        print(f"\n  If {w} winners and {l} losers:")
        print(f"  mean_r = (sum_wins - abs(sum_losses)) / n")
        
        # Try to find winning/losing R values
        # With PF = 3.518, we have: sum_wins / abs(sum_losses) = 3.518
        # And: (sum_wins + sum_losses) / n = mean_r = 2.046
        # Let's solve:
        # sum_wins = 3.518 * abs(sum_losses)
        # (3.518 * abs(sum_losses) + sum_losses) / 32 = 2.046  -- but sum_losses is negative
        # Actually: (sum_wins - abs(sum_losses)) / 32 = 2.046
        # And: sum_wins / abs(sum_losses) = 3.518
        
        # From PF: sum_wins = 3.518 * gl where gl = abs(sum_losses)
        # From mean: (sum_wins + sum_losses) / 32 = mean_r
        # But sum_losses = -gl, so: (sum_wins - gl) / 32 = mean_r
        # Substituting: (3.518*gl - gl) / 32 = mean_r
        # gl * (3.518 - 1) / 32 = mean_r
        # gl * 2.518 / 32 = 2.046
        # gl = 2.046 * 32 / 2.518 = 25.97
        # sum_wins = 3.518 * 25.97 = 91.38
        # Check: (91.38 - 25.97) / 32 = 65.41 / 32 = 2.044 ✓
        
        gl = r['expectancy_r'] * n / (r['profit_factor'] - 1)
        sum_wins = r['profit_factor'] * gl
        print(f"  Implied gross wins: {sum_wins:.2f}R")
        print(f"  Implied gross losses: {gl:.2f}R")
        print(f"  Per-trade average win: {sum_wins/w:.2f}R (if {w} winners)")
        print(f"  Per-trade average loss: {gl/l:.2f}R (if {l} losers)")
        
        # Now check: what if the losses are small and wins are large?
        # That would explain low win rate but high expectancy
        print(f"\n  Key insight: {w} winners generate {sum_wins:.2f}R total,")
        print(f"  while {l} losers absorb {gl:.2f}R total.")
        print(f"  Average win: {sum_wins/w:.2f}R, Average loss: {gl/l:.2f}R")
        print(f"  Win/loss ratio: {(sum_wins/w)/(gl/l):.2f}")
        print(f"  This means: each winner averages {(sum_wins/w)/(gl/l):.2f}x the average loss.")
        break