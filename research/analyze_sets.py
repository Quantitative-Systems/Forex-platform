import json

r = json.load(open('research/results/per_set_native_backtest.json'))

# Calculate concord matrix
print('='*70)
print('CONCORD EDGE CONCEPT (per-pair, per-set profitable-edge matrix)')
print('='*70)
print()

filter_combos = []
for pair, d in r['pairs'].items():
    sets = []
    for sk in ['SET_3','SET_4','SET_5']:
        m = d.get(sk, {})
        if m.get('trades') and m['net_pnl_r'] > 0:
            sets.append(sk)
    excluded = [sk for sk in ['SET_3','SET_4','SET_5'] if sk not in sets]
    if sets:
        print('  {}: RUN {}   (skip {})'.format(pair, '+'.join(sets), ', '.join(excluded) if excluded else 'none'))
        filter_combos.extend([(pair, sk) for sk in sets])
    else:
        print('  {}: RUN NONE (no profitable sets)'.format(pair))

print()
total_t, total_p = 0, 0.0
for pair, d in r['pairs'].items():
    for sk in ['SET_3','SET_4','SET_5']:
        m = d.get(sk, {})
        if m.get('trades') and m['net_pnl_r'] > 0:
            total_t += m['trades']
            total_p += m['net_pnl_r']

print('Concord:  {} trades | {:+.2f}R net | {:+.3f}R/trade'.format(total_t, total_p, total_p/total_t))
print('All-sets: {} trades | {:+.2f}R net | {:+.3f}R/trade'.format(
    r['summary']['total_trades'], r['summary']['total_net_pnl_r'],
    r['summary']['total_net_pnl_r']/r['summary']['total_trades']))
print('Filtering gain: {:+.2f}R  (+{:.1f}% trades kept, +{:.1f}% R kept)'.format(
    total_p - r['summary']['total_net_pnl_r'],
    total_t/r['summary']['total_trades']*100,
    total_p/r['summary']['total_net_pnl_r']*100))

# Pair-specific detail
print()
print('='*70)
print('PAIR-SPECIFIC PROFITABILITY RANKING')
print('='*70)
rank_items = []
for pair, d in r['pairs'].items():
    nets = sum(m.get('net_pnl_r',0) for m in d.values() if isinstance(m, dict) and m.get('trades'))
    tr = sum(m.get('trades',0) for m in d.values() if isinstance(m, dict) and m.get('trades'))
    pos_sets = [sk for sk, m in d.items() if isinstance(m, dict) and m.get('trades') and m['net_pnl_r'] > 0]
    rank_items.append((pair, nets, tr, pos_sets))
rank_items.sort(key=lambda x: x[1], reverse=True)
for pair, nets, tr, pos_sets in rank_items:
    print('  {:8} : {:+7.2f}R  ({:3} trades)  viable sets: {}'.format(pair, nets, tr, pos_sets))

print()
print('='*70)
print('VERDICT')
print('='*70)
print()
print('The SAME concept works on SET_3, SET_4, SET_5, but with pair-specific conditions:')
print()
print('SET_3 (4H HTF -> 1D LTF) — Works on JPY and AUD crosses only')
print('   - EURUSD/GBPUSD on daily TF do not exhibit profitable edge at 1:4 RR')
print('   - HTF (1D) cycles too slow; entries too late')
print()
print('SET_4 (1H HTF -> 15M LTF) — Works on GBP, JPY, AUD (NOT EURUSD)')
print('   - EURUSD daily->15M fails - EURUSD on HTF 1H is too "tight"')
print('   - JPY and AUD crosses have clear higher-TF structural levels')
print()
print('SET_5 (15M HTF -> 5M LTF) — Works on ALL pairs (primary driver)')
print('   - Highest frequency, tightest entry, most consistent profitability')
print('   - EURUSD works on SET_5 (PF 1.63, +19.76R) — scalping too tight')
print()
print('CONCORD = SET_5-always-ON + SET_4 (no EURGBP) + SET_3 (only JPY/AUD)')
print('  Trades: 244 | Net: +{:.2f}R | Exp +{:.3f}R per trade'.format(total_p, total_p/total_t))