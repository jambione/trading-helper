#!/usr/bin/env bash
cd "$(dirname "$0")"

# Use the project venv (Python 3.12 + MLX Whisper) if it exists,
# otherwise fall back to system python3.
if [ -x "venv/bin/python" ]; then
    PYTHON="venv/bin/python"
else
    echo "WARNING: venv not found — MLX Whisper unavailable. Run:"
    echo "  /opt/homebrew/bin/python3.12 -m venv venv && venv/bin/pip install -r requirements.txt mlx-whisper"
    PYTHON="python3"
fi

exec "$PYTHON" start_all.py
