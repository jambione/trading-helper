#!/bin/zsh
# Nightly storage upkeep (operator 11/21 "We should optimize the mini storage"): gzip admit-ledger days and large tape
# logs older than 7 days. A ledger day that is also hard-linked into ~/session_snapshots/DAY/admit_ledger.jsonl is
# compressed once and both names point to the one .gz. Each file is verified (gzip -t and byte count) before the
# original name is removed. Readers use smart_open.py (FILE or FILE.gz).
set -u
ROOT=${0:A:h:h}
CUT=$(date -v-7d +%Y-%m-%d)
cd "$ROOT/ai_reports/admit_ledger" 2>/dev/null && for f in 2026-*.jsonl(N); do
  d=${f%.jsonl}; [[ "$d" < "$CUT" ]] || continue
  s=~/session_snapshots/$d/admit_ledger.jsonl
  if [[ -f "$s" && "$(stat -f %i "$f")" != "$(stat -f %i "$s")" ]]; then echo "skip $d (different snapshot file)"; continue; fi
  nice -n 15 gzip -c "$f" > "$f.gz" && gzip -t "$f.gz" && [[ "$(gzip -cd "$f.gz" | wc -c | tr -d ' ')" == "$(stat -f %z "$f")" ]] || { rm -f "$f.gz"; echo "FAIL $d"; continue; }
  rm "$f"; [[ -f "$s" ]] && { rm "$s"; ln "$f.gz" "$s.gz"; }
  echo "ledger $d compressed"
done
cd "$ROOT/ai_reports/tapes" 2>/dev/null && for dir in 2026-*(N/); do
  [[ "${dir:0:10}" < "$CUT" ]] || continue
  for f in $dir/*.jsonl(N.L+20480); do
    nice -n 15 gzip "$f" && gzip -t "$f.gz" && echo "tape $f compressed" || echo "FAIL $f"
  done
done
exit 0
