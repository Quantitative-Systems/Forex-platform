import json
import hashlib
import os

audit_dir = 'research/audits/provenance_audit_20261014_02'
snapshot_dir = os.path.join(audit_dir, 'snapshot_GBPUSD_SET5_SCA-LPING_v1')

# Build artifact index
index = {
    'audit_id': 'audit_20261014_02',
    'generated_utc': '2026-10-14T04:10:52+00:00',
    'candidate': 'GBPUSD|SET_5|SCALPING',
    'git_sha': '38611167c0e2851c14e269a63e901d41b9d2a135',
    'artifacts': {},
    'snapshot': {},
}

# Add top-level audit files
for f in ['EXECUTIVE_REPORT.md', 'provenance_manifest.json', 'metrics_recalculation.json',
          'causal_audit.md', 'shared_event_trace.json', 'test_results.md',
          'shadow_validation_protocol.md', 'macro_deep_history_status.md']:
    path = os.path.join(audit_dir, f)
    if os.path.exists(path):
        with open(path, 'rb') as fh:
            sha = hashlib.sha256(fh.read()).hexdigest()
        index['artifacts'][f] = {
            'path': os.path.relpath(path, audit_dir),
            'sha256': sha,
            'size': os.path.getsize(path),
        }

# Add snapshot files
for f in ['config.json', 'dataset_manifest.json', 'metrics_recalculation.json',
          'trade_ledger_synthetic.json', 'causal_audit_summary.json', 'integrity_hashes.json']:
    path = os.path.join(snapshot_dir, f)
    if os.path.exists(path):
        with open(path, 'rb') as fh:
            sha = hashlib.sha256(fh.read()).hexdigest()
        index['snapshot'][f] = {
            'path': os.path.relpath(path, audit_dir),
            'sha256': sha,
            'size': os.path.getsize(path),
        }

# Write the artifact index
with open(os.path.join(audit_dir, 'artifact_index.json'), 'w') as f:
    json.dump(index, f, indent=2)

print("=== Artifact index created ===")
print(f"Top-level artifacts: {len(index['artifacts'])} files")
for name, info in index['artifacts'].items():
    print(f"  {name}: {info['path']} (sha256: {info['sha256'][:16]}...)")
print(f"Snapshot artifacts: {len(index['snapshot'])} files")
for name, info in index['snapshot'].items():
    print(f"  {name}: {info['path']} (sha256: {info['sha256'][:16]}...)")