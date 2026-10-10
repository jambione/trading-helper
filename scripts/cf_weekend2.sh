#!/bin/zsh
# Weekend counterfactual batch 2 (operator 10/10; INFORMATION ONLY, seen sessions): %R and RSI direction entry x exits that
# do not stomp runs, all on the live presquare-only entry. Waits for batch 1 (/tmp/cf/status.txt ALL DONE), two lanes by day.
# Results: /tmp/cf/REPORT2.txt
cd ~/repo/trading-helper || exit 1
until grep -q "ALL DONE" /tmp/cf/status.txt 2>/dev/null; do sleep 120; done
git pull -q --ff-only
echo "start $(date)" > /tmp/cf/status2.txt
DAYS=(2026-09-24 2026-09-25 2026-09-28 2026-09-29 2026-09-30 2026-10-01 2026-10-02 2026-10-05 2026-10-06 2026-10-07 2026-10-08 2026-10-09)
PQ=(--set ai_watch_presquare_only=true)
WR=(--set ai_watch_wr_trend_min_rise=15)
RSI=(--set ai_watch_wr_rsi_min_rise=10)
NP=(--set ai_no_progress_flatten_enabled=true --set ai_no_progress_sec=180 --set ai_no_progress_mfe_r=0.001)
LOB=(--set ai_exit_left_overbought=true)
ND=(--set ai_local_trail_time_decay_enabled=false)
ST=(--set ai_local_trail_time_decay_enabled=false --set ai_exit_supertrend=true)
R() { local d=$1 n=$2; shift 2; [ -f /tmp/cf/$d-$n.json ] && return; nice -n 10 .venv/bin/python tools/replay_session.py --day $d "$@" --out /tmp/cf/$d-$n.json > /tmp/cf/$d-$n.log 2>&1; }
lane() {
  for d in "$@"; do
    R $d presq_only $PQ
    R $d pq_wr15 $PQ $WR
    R $d pq_wrrsi $PQ $WR $RSI
    R $d pq_np_lob $PQ $NP $LOB
    R $d pq_st $PQ $NP $LOB $ST
    R $d pq_nodecay $PQ $NP $LOB $ND
    R $d pq_wrrsi_np_lob $PQ $WR $RSI $NP $LOB
    R $d pq_wrrsi_st $PQ $WR $RSI $NP $LOB $ST
    R $d pq_wrrsi_nodecay $PQ $WR $RSI $NP $LOB $ND
    echo "$d done $(date)" >> /tmp/cf/status2.txt
  done
}
lane ${DAYS[1]} ${DAYS[3]} ${DAYS[5]} ${DAYS[7]} ${DAYS[9]} ${DAYS[11]} &
lane ${DAYS[2]} ${DAYS[4]} ${DAYS[6]} ${DAYS[8]} ${DAYS[10]} ${DAYS[12]} &
wait
S() { nice -n 10 .venv/bin/python tools/studies/cf_score.py --dir /tmp/cf "$@" $DAYS; }
{
  echo "===== ENTRY filters (live exits), reference presq_only"
  S --ref presq_only --vars pq_wr15 pq_wrrsi
  echo; echo "===== EXITS that do not stomp runs (no entry filter), reference pq_np_lob"
  S --ref pq_np_lob --vars pq_st pq_nodecay
  echo; echo "===== EXITS with the %R+RSI entry, reference pq_wrrsi_np_lob"
  S --ref pq_wrrsi_np_lob --vars pq_wrrsi_st pq_wrrsi_nodecay
  echo; echo "===== COMBINED vs the live-like presquare desk, reference presq_only"
  S --ref presq_only --vars pq_np_lob pq_wrrsi_np_lob pq_st pq_wrrsi_st pq_wrrsi_nodecay
} > /tmp/cf/REPORT2.txt 2>&1
echo "ALL DONE $(date)" >> /tmp/cf/status2.txt
