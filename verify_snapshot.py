import json
import hashlib
import os

snapshot_dir = 'research/audits/provenance_audit_20261014_02/snapshot_GBPUSD_SET5_SCA-LPING_v1'

# List all files in snapshot
print("=== Snapshot files ===")
for root, dirs, files in os.walk(snapshot_dir):
    for f in files:
        path = os.path.join(root, f)
        print(path)

# Verify read-back hashes
print("\n=== Read-back verification ===")
verification = {}
for name in ['config.json', 'dataset_manifest.json', 'metrics_recalculation.json', 'trade_ledger_synthetic.json', 'causal_audit_summary.json']:
    path = os.path.join(snapshot_dir, name)
    if os.path.exists(path):
        with open(path, 'rb') as f:
            read_back = hashlib.sha256(f.read()).hexdigest()
        print(f"{name}: read_back={read_back[:16]}...")
        verification[name] = read_back
    else:
        print(f"{name}: MISSING")

# Read the integrity hashes file
integrity_path = os.path.join(snapshot_dir, 'integrity_hashes.json')
if os.path.exists(integrity_path):
    with open(integrity_path) as f:
        integrity = json.load(f)
    print("\n=== Integrity file verification ===")
    for name, info in integrity.get('readback_verification', {}).items():
        print(f"{name}: recorded={info['read_back'][:16]}... read_back={info['recorded'][:16]}... match={info['match']}")