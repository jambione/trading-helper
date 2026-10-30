#!/bin/zsh
# orb_in_play amended_5 long-history fetch + outcome-blind count. Run AFTER buystop_flow's fetch/count (operator 10/30:
# buystop first). On the mini:  nohup scripts/orb_fetch_chain.sh > logs/orb_fetch_chain.log 2>&1 &
cd ~/repo/trading-helper || exit 1
say() { echo "[orb] $(TZ=America/New_York date +%H:%M:%S) $*"; }
if pgrep -f "buystop_flow.py" > /dev/null; then echo "buystop_flow is running; wait for it (one heavy job at a time)"; exit 1; fi
# orb_in_play amended_5 (READY; code review round 2 READY TO FETCH): long-history fetch, then the outcome-blind count.
# Runs into the weekend (no desk). The 10/24 files are archived so nothing old is reused.
ORB=ai_reports/orb_in_play
if [ -f $ORB/open.pkl ] && [ ! -f $ORB/open_union.pkl ]; then
  mkdir -p $ORB/archive_1024 && mv $ORB/*.pkl $ORB/*.json $ORB/archive_1024/ 2>/dev/null
  say "archived the 10/24 ORB files"
fi
.venv/bin/python tools/studies/orb_in_play.py --fetch-daily && say "orb daily done" \
 && .venv/bin/python tools/studies/orb_in_play.py --fetch-open && say "orb open done" \
 && .venv/bin/python tools/studies/orb_in_play.py --fetch-day && say "orb fullday done" \
 && .venv/bin/python tools/studies/orb_in_play.py --prereg-commit "$(git rev-parse --short HEAD)" && say "orb count done"
say "done"
