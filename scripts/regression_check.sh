#!/bin/zsh
# Regression guard for the CODE (operator 11/05): run the full test suite in a throwaway worktree (the suite rewrites
# config/bot_config.json, so never run it in the live checkout) and compare the failing set with tests/known_failures.txt
# (+ every test_sim_rstop_path test, known broken). Prints NEW FAILURES / FIXED and exits 1 on any new failure.
# usage: scripts/regression_check.sh      (from the repo root; takes a few minutes)
cd "$(dirname "$0")/.." || exit 2
wt=$(mktemp -d /tmp/regcheck.XXXXXX)
git worktree add -q --detach "$wt" HEAD || exit 2
cd "$wt"
py=$(ls ~/repo/trading-helper/.venv/bin/python 2>/dev/null || command -v python3)
$py -m pytest -q --no-header -p no:cacheprovider -rf --maxfail=200 2>&1 | grep '^FAILED' | sed 's/^FAILED //; s/ - .*//' | sort -u > "$wt/failing.txt"
known=$(grep -v test_sim_rstop_path "$OLDPWD/tests/known_failures.txt" | sort -u)
new=$(grep -v test_sim_rstop_path "$wt/failing.txt" | sort -u | comm -23 - <(echo "$known"))
fixed=$(comm -13 <(grep -v test_sim_rstop_path "$wt/failing.txt" | sort -u) <(echo "$known") | grep .)
cd "$OLDPWD"; git worktree remove --force "$wt"
[ -n "$fixed" ] && echo "FIXED (remove from tests/known_failures.txt):\n$fixed"
if [ -n "$new" ]; then echo "NEW FAILURES:\n$new"; exit 1; fi
echo "regression_check OK: no new failing tests"
