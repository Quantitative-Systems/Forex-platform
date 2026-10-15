import os

audit_dir = 'research/audits/provenance_audit_20261014_02'
required = [
    'EXECUTIVE_REPORT.md',
    'provenance_manifest.json',
    'metrics_recalculation.json',
    'causal_audit.md',
    'shared_event_trace.json',
    'test_results.md',
    'shadow_validation_protocol.md',
    'macro_deep_history_status.md',
]

print("=== Audit package files ===")
for f in required:
    path = os.path.join(audit_dir, f)
    if os.path.exists(path):
        size = os.path.getsize(path)
        print(f"  OK {f}: {size} bytes")
    else:
        print(f"  MISSING {f}")

# Artifact index
index_path = os.path.join(audit_dir, 'artifact_index.json')
if os.path.exists(index_path):
    print(f"\n  OK artifact_index.json: exists")
else:
    print(f"\n  MISSING artifact_index.json - needs creation")