#!/bin/bash
# hist_sim_prereg amended_4: rebuild the UNTOUCHED calibration days 2026-09-24..10-02 with
# hist_universe u2 and score recall once. Mini only; waits for 16:15 ET (spread lookups).
#   nohup bash scripts/hist_universe_u2_check.sh > /tmp/hu_u2.log 2>&1 &
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=ai_reports/hist_universe
mkdir -p "$OUT/u1_archive"
cp -n "$OUT"/calib_2026-09-2*.json "$OUT"/calib_2026-09-30.json "$OUT"/calib_2026-10-0[12].json "$OUT"/check.json "$OUT/u1_archive/" 2>/dev/null || true
while [ "$(TZ=America/New_York date +%H%M)" -lt 1615 ] && [ "$(TZ=America/New_York date +%H%M)" -ge 0330 ]; do sleep 120; done
.venv/bin/python - <<'PY'
import sys, json
sys.path.insert(0, "tools/studies")
import hist_universe as HU
assert HU.RULE_VERSION.startswith("hist_universe/u2"), HU.RULE_VERSION
HU.cmd_check("2026-09-24..2026-10-02", out_name="check_u2_untouched")
PY
