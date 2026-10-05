#!/bin/zsh
# Night batch for 2026-10-05. Run ON THE MINI after 16:05 ET (SIP is 15 min delayed), from the repo root:
#   cd ~/repo/trading-helper && git pull --ff-only && nohup zsh scripts/night_2026-10-05.sh > /tmp/night.log 2>&1 &
# Read-only studies + replays: no orders, no config edits, no restarts. Outputs in /tmp/rp_lob/ and ai_reports/.
cd ~/repo/trading-helper || exit 1
mkdir -p /tmp/rp_lob
echo "== 1 order-block skip test (prereg docs/studies/order_block_gate_prereg.json)"
.venv/bin/python tools/studies/order_block_gate.py > /tmp/rp_lob/order_block_gate.txt 2>&1
echo "== 2 support->resistance range trade (prereg docs/studies/sr_range_trade_prereg.json)"
.venv/bin/python tools/studies/sr_range_trade.py > /tmp/rp_lob/sr_range.txt 2>&1
echo "== 3 held-out: today's fills under the order-block rule"
.venv/bin/python tools/studies/ob_fills_daily.py 2026-10-05 > /tmp/rp_lob/ob_fills.txt 2>&1
echo "== 4 overbought-exit replay: each day as live (base) vs ai_exit_left_overbought=true (lob)"
for d in 2026-09-29 2026-09-30 2026-10-01 2026-10-02 2026-10-05; do
  .venv/bin/python tools/replay_session.py --day $d --out /tmp/rp_lob/$d-base.json > /tmp/rp_lob/$d-base.log 2>&1
  .venv/bin/python tools/replay_session.py --day $d --set ai_exit_left_overbought=true --out /tmp/rp_lob/$d-lob.json > /tmp/rp_lob/$d-lob.log 2>&1
done
.venv/bin/python tools/studies/replay_costing.py /tmp/rp_lob/*.json > /tmp/rp_lob/costing.txt 2>&1
echo NIGHT_DONE > /tmp/rp_lob/done
