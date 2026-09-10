#!/usr/bin/env bash
# Run every claude-voiceover test. No framework: each test file is a script
# that prints PASS/FAIL lines and exits non-zero on any failure.
set -u
cd "$(dirname "$0")/.."
status=0
for test_file in tests/test_*.py; do
    echo "=== $test_file ==="
    if ! python3 "$test_file"; then
        status=1
    fi
    echo
done
if [ "$status" -eq 0 ]; then
    echo "ALL TESTS PASSED"
else
    echo "SOME TESTS FAILED"
fi
exit "$status"
