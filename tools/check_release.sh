#!/usr/bin/env bash
# GNU timeout applies one eight-second wall-clock budget to all interpreters.
set -euo pipefail
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
pids=()
for version in 3.11 3.12 3.13; do
  "$RUNNER_TEMP/meltygui-check-$version/bin/python" -I -m pytest \
    tests/test_package_contract.py tests/test_public_boundary.py \
    tests/test_core_syntax.py::TestScannerParity::test_sample \
    --noconftest -q --tb=short --durations=3 > "$RUNNER_TEMP/meltygui-test-$version.log" 2>&1 &
  pids+=("$!")
done
status=0
for pid in "${pids[@]}"; do
  wait "$pid" || status=1
done
for version in 3.11 3.12 3.13; do
  echo "Python $version"
  cat "$RUNNER_TEMP/meltygui-test-$version.log"
done
exit "$status"
