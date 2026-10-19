#!/bin/bash
# Floor-$2 short sweep (docs/studies/floor2_short_prereg.json), mini only. Replays the live desk with the price floor
# at $2 and 20 seats, then scores every open as a short with fill_short --replay-dir. Pins the whole live config of
# HEAD. Never launches 03:30-16:15 ET unless SWEEP_DAYTIME=1 with the data-only account.
#   nohup bash scripts/floor2_sweep.sh [OUT] > /tmp/floor2_run.log 2>&1 &
set -euo pipefail
cd "$(dirname "$0")/.."
SHA=${SWEEP_SHA:-$(git rev-parse --short HEAD)}
OUT=${1:-/tmp/floor2_$(date +%Y%m%d)}
mkdir -p "$OUT"
MAXP=${SWEEP_MAXP:-6}
git show "$SHA:config/bot_config.json" > "$OUT/base_config.json"
N=$(.venv/bin/python -c "import json,sys; print(len(json.load(open(sys.argv[1]))))" "$OUT/base_config.json")
if [ "$N" -lt 300 ]; then echo "base config has only $N keys - refusing to run unpinned"; exit 1; fi
[ -n "${SWEEP_SHA:-}" ] || git diff --quiet HEAD -- || { echo "tracked files differ from HEAD - commit first"; exit 1; }
echo "sha $SHA keys $N out $OUT"
NICE=""
if [ -n "${SWEEP_DAYTIME:-}" ]; then
  [ -f "${TH_DATA_SECRETS:-}" ] || { echo "SWEEP_DAYTIME needs TH_DATA_SECRETS=<secrets_data.json>"; exit 1; }
  export TH_DATA_SECRETS
  NICE="nice -n 19"
else
  while [ "$(TZ=America/New_York date +%H%M)" -lt 1615 ] && [ "$(TZ=America/New_York date +%H%M)" -ge 0330 ]; do sleep 120; done
fi

FLOOR="--set ai_watch_profile_path= --set ai_watch_min_price=2.0 --set ai_watch_momentum_min_price=2.0 \
  --set ai_watch_movers_min_price=2.0 --set ai_tight_min_price=2.0 --set rs_min_price=2.0 --set h4_min_price=2.0 \
  --set ai_max_positions=20 --set ai_max_position_pct=5"
A=floor2
DAYS="2026-10-06 2026-10-07 2026-10-08 2026-10-09 2026-10-12 2026-10-13 2026-10-14 2026-10-15 2026-10-16"
for d in $DAYS; do
  [ -s "$OUT/${d}_${A}.json" ] && continue
  pgrep -f -- "--out $OUT/${d}_${A}.json" > /dev/null && continue
  if [ -z "${SWEEP_DAYTIME:-}" ]; then
    HM=$(TZ=America/New_York date +%H%M)
    if [ "$HM" -ge 0330 ] && [ "$HM" -lt 1615 ]; then echo "deadline: not launched $d $A" >> "$OUT/failures.txt"; continue; fi
  fi
  while [ "$(pgrep -f 'tools/replay_session.py --day' | wc -l)" -ge $((MAXP * 2)) ]; do sleep 20; done
  # shellcheck disable=SC2086
  ( $NICE .venv/bin/python tools/replay_session.py --day "$d" --sha "$SHA" --set-json "$OUT/base_config.json" \
      $FLOOR --out "$OUT/${d}_${A}.json" > "$OUT/${d}_${A}.log" 2>&1 \
    || echo "FAILED $d $A" >> "$OUT/failures.txt" ) &
  sleep 5
done
wait
[ -f "${TH_DATA_SECRETS:-}" ] || { echo "replays done; score with TH_DATA_SECRETS=<secrets_data.json> .venv/bin/python tools/studies/fill_short.py --replay-dir $OUT"; exit 0; }
$NICE .venv/bin/python tools/studies/fill_short.py --replay-dir "$OUT" 2>"$OUT/score.log" | tee "$OUT/result.md"
