#!/bin/bash
# hist_sim_prereg amended_4: rebuild the UNTOUCHED calibration days 2026-09-24..10-02 with the
# current hist_universe rule version and score recall ONCE. Graded: quote_mode 'file' (never more
# generous than live). Information: quote_mode 'rt' (upper bound). Mini only; waits for 16:15 ET.
#   nohup bash scripts/hist_universe_u2_check.sh > /tmp/hu_u2.log 2>&1 &
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=ai_reports/hist_universe
mkdir -p "$OUT/u1_archive"
for f in "$OUT"/calib_2026-09-2[4-9].json "$OUT"/calib_2026-09-30.json "$OUT"/calib_2026-10-0[12].json "$OUT"/check.json; do
  [ -f "$f" ] && cp -n "$f" "$OUT/u1_archive/" || true
done
while :; do HM=$(TZ=America/New_York date +%H%M); { [ "$HM" -ge 1615 ] || [ "$HM" -lt 0330 ]; } && break; sleep 120; done
.venv/bin/python - <<'PY'
import sys, json
sys.path.insert(0, "tools/studies")
import hist_universe as HU
assert HU.RULE_VERSION.startswith("hist_universe/u2"), HU.RULE_VERSION
span = "2026-09-24..2026-10-02"
graded = HU.cmd_check(span, knobs={"quote_mode": "file"}, out_name="check_u2_untouched_file_GRADED")
info = HU.cmd_check(span, knobs={"quote_mode": "rt"}, out_name="check_u2_untouched_rt_info")
print("GRADED (file):", json.dumps({k: graded[k] for k in ("pooled_pct", "pooled_pct_without_exclusion", "pass_pooled", "void", "git_sha")}))
print("INFO (rt):", json.dumps({k: info[k] for k in ("pooled_pct", "pass_pooled", "void")}))
PY
