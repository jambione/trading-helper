#!/bin/bash
# Turn-open sweep (docs/studies/turn_open_prereg.json), mini only. Pins the whole live config of HEAD and varies
# only the turn arms. Never launches 03:30-16:15 ET unless SWEEP_DAYTIME=1 with the data-only account.
#   nohup bash scripts/turn_open_sweep.sh [OUT] > /tmp/turn_open_run.log 2>&1 &
set -euo pipefail
cd "$(dirname "$0")/.."
SHA=${SWEEP_SHA:-$(git rev-parse --short HEAD)}
OUT=${1:-/tmp/turn_open_$(date +%Y%m%d)}
mkdir -p "$OUT"
MAXP=${SWEEP_MAXP:-6}
git show "$SHA:config/bot_config.json" > "$OUT/base_config.json"
N=$(.venv/bin/python -c "import json,sys; print(len(json.load(open(sys.argv[1]))))" "$OUT/base_config.json")
if [ "$N" -lt 300 ]; then echo "base config has only $N keys - refusing to run unpinned"; exit 1; fi
[ -n "${SWEEP_SHA:-}" ] || git diff --quiet HEAD -- || { echo "tracked files differ from HEAD - commit first"; exit 1; }
git grep -q "_any_turn_allows_buy" "$SHA" -- ai_entry_watch.py || { echo "commit $SHA has no any-turn lane"; exit 1; }
echo "sha $SHA keys $N out $OUT"
NICE=""
if [ -n "${SWEEP_DAYTIME:-}" ]; then
  [ -f "${TH_DATA_SECRETS:-}" ] || { echo "SWEEP_DAYTIME needs TH_DATA_SECRETS=<secrets_data.json>"; exit 1; }
  export TH_DATA_SECRETS
  NICE="nice -n 19"
else
  while [ "$(TZ=America/New_York date +%H%M)" -lt 1615 ] && [ "$(TZ=America/New_York date +%H%M)" -ge 0330 ]; do sleep 120; done
fi

arm_sets() {
  local BASE="--set ai_watch_profile_path="
  local TURN="$BASE --set ai_watch_exh_any_turn_arm=true --set ai_watch_arm_cm_rsi_max=100 --set ai_watch_presquare_only=false"
  case $1 in
    live)   echo "$BASE" ;;
    turn5)  echo "$TURN" ;;
    turn20) echo "$TURN --set ai_max_positions=20 --set ai_max_position_pct=5" ;;
    *) echo "unknown arm $1" >&2; exit 1 ;;
  esac
}

DAYS="2026-10-06 2026-10-07 2026-10-08 2026-10-09 2026-10-12 2026-10-13 2026-10-14 2026-10-15 2026-10-16"
for a in live turn5 turn20; do
  for d in $DAYS; do
    [ -s "$OUT/${d}_${a}.json" ] && continue
    pgrep -f -- "--out $OUT/${d}_${a}.json" > /dev/null && continue
    if [ -z "${SWEEP_DAYTIME:-}" ]; then
      HM=$(TZ=America/New_York date +%H%M)
      if [ "$HM" -ge 0330 ] && [ "$HM" -lt 1615 ]; then echo "deadline: not launched $d $a" >> "$OUT/failures.txt"; continue; fi
    fi
    while [ "$(pgrep -f 'tools/replay_session.py --day' | wc -l)" -ge $((MAXP * 2)) ]; do sleep 20; done
    # shellcheck disable=SC2046
    ( $NICE .venv/bin/python tools/replay_session.py --day "$d" --sha "$SHA" --set-json "$OUT/base_config.json" \
        $(arm_sets "$a") --out "$OUT/${d}_${a}.json" > "$OUT/${d}_${a}.log" 2>&1 \
      || echo "FAILED $d $a" >> "$OUT/failures.txt" ) &
    sleep 5
  done
done
wait
$NICE .venv/bin/python tools/studies/turn_open_score.py --dir "$OUT" | tee "$OUT/result.md"
