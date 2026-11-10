#!/bin/sh
# Installs the commit-msg hook that refuses a config/bot_config.json commit with no "Evidence:" line. Run once per checkout.
cd "$(git rev-parse --show-toplevel)" || exit 1
cat > .git/hooks/commit-msg <<'H'
#!/bin/sh
PY=.venv/bin/python; [ -x "$PY" ] || PY=python3
exec "$PY" scripts/check_config_evidence.py --commit-msg "$1"
H
chmod +x .git/hooks/commit-msg && echo "commit-msg hook installed"
