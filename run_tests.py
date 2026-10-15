import json
import subprocess
import sys
import os

def run_test(test_file):
    """Run a test file and capture results."""
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', test_file, '-v', '--tb=short'],
        capture_output=True,
        text=True,
        cwd='C:\\Users\\nares\\Workspace\\Forex-platform'
    )
    return {
        'file': test_file,
        'exit_code': result.returncode,
        'stdout': result.stdout[-2000:] if len(result.stdout) > 2000 else result.stdout,
        'stderr': result.stderr[-2000:] if len(result.stderr) > 2000 else result.stderr,
        'passed': 0,
        'failed': 0,
        'skipped': 0
    }

# Discover test counts from stdout
def parse_test_counts(stdout):
    # Try to extract passed/failed/skipped
    counts = {'passed': 0, 'failed': 0, 'skipped': 0}
    import re
    m = re.search(r'(\d+) passed', stdout)
    if m:
        counts['passed'] = int(m.group(1))
    m = re.search(r'(\d+) failed', stdout)
    if m:
        counts['failed'] = int(m.group(1))
    m = re.search(r'(\d+) skipped', stdout)
    if m:
        counts['skipped'] = int(m.group(1))
    return counts

# Run relevant tests
test_files = [
    'tests/unit/test_fractal_institutional.py',
    'tests/unit/test_costs.py',
    'tests/unit/test_qualification_gates.py',
    'tests/unit/test_causal_data.py',
    'tests/unit/test_fractal_engine.py',
]

results = []
for tf in test_files:
    if os.path.exists(tf):
        r = run_test(tf)
        counts = parse_test_counts(r['stdout'] + r['stderr'])
        r['passed'] = counts['passed']
        r['failed'] = counts['failed']
        r['skipped'] = counts['skipped']
        results.append(r)
        print(f"\n{'='*60}")
        print(f"TEST: {tf}")
        print(f"Exit code: {r['exit_code']}")
        print(f"Passed: {r['passed']}, Failed: {r['failed']}, Skipped: {r['skipped']}")
        print(f"STDOUT (last 500 chars): {r['stdout'][-500:]}")
        print(f"STDERR (last 500 chars): {r['stderr'][-500:]}")
    else:
        print(f"SKIP: {tf} not found")

# Summary
print(f"\n{'='*60}")
print("TEST SUMMARY")
print(f"{'='*60}")
total_passed = sum(r['passed'] for r in results)
total_failed = sum(r['failed'] for r in results)
total_skipped = sum(r['skipped'] for r in results)
print(f"Total passed: {total_passed}")
print(f"Total failed: {total_failed}")
print(f"Total skipped: {total_skipped}")
print(f"Total tests: {total_passed + total_failed + total_skipped}")