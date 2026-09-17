#!/usr/bin/env bash
# Fails if the browser implementation and the Python package disagree on a
# mandate id. Skips cleanly when node is not installed.
set -euo pipefail
cd "$(dirname "$0")/.."
command -v node >/dev/null || { echo "SKIP: node not installed"; exit 0; }

node scripts/cross_check_id.mjs > /tmp/mandate-js.txt
python3 - > /tmp/mandate-py.txt <<'PY'
from core import spec
m = spec.Mandate(
    name="x", strategist="0x" + "11" * 20, thesis="t",
    legs=(spec.Leg("NVDA", "0xd0601CE157Db5bdC3162BbaC2a2C8aF5320D9EEC", 4000),
          spec.Leg("META", "0xAAAA000000000000000000000000000000000001", 3000),
          spec.Leg("SPY", "0xBBBB000000000000000000000000000000000002", 3000)),
    execution=spec.Execution(kind="twap", slices=4, interval_seconds=900,
                             max_slippage_bps=50, min_depth_multiple=3,
                             require_market_hours=False),
    risk=spec.Risk(800, 1500, 10000, 12), fee_bps=100, chain_id=4663)
m.validate()
print(m.canonical().decode())
print(m.mandate_id())
PY

if diff -q /tmp/mandate-js.txt /tmp/mandate-py.txt >/dev/null; then
  echo "OK: javascript and python agree"
  tail -1 /tmp/mandate-py.txt
else
  echo "FAIL: implementations disagree"
  diff /tmp/mandate-js.txt /tmp/mandate-py.txt || true
  exit 1
fi
