#!/usr/bin/env python3
"""Start the Signal Scanner dashboard + signal engine.

Both processes run together; Ctrl+C or either process exiting stops both.
Uses venv when available, otherwise falls back to the system python3.
"""
import os
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
VENV_BIN = os.path.join(ROOT, "venv", "bin")
VENV_PYTHON = os.path.join(VENV_BIN, "python")

# Fall back to system python3 if no venv exists
if not os.path.isfile(VENV_PYTHON):
    import shutil
    VENV_PYTHON = shutil.which("python3") or sys.executable


class _ProcExited(Exception):
    """Raised internally when any managed subprocess exits, to break the wait loop."""


def _stream(proc: subprocess.Popen, label: str) -> None:
    assert proc.stdout
    for raw in iter(proc.stdout.readline, b''):
        sys.stdout.write(f'{label} {raw.decode(errors="replace")}')
        sys.stdout.flush()


def main() -> None:
    utf8_env = {
        **os.environ,
        'PYTHONUTF8': '1',
        'VIRTUAL_ENV': os.path.join(ROOT, 'venv'),
        'PATH': f"{VENV_BIN}:{os.environ.get('PATH', '')}",
    }

    # ── Dashboard ─────────────────────────────────────────────────────────────
    dashboard = subprocess.Popen(
        [VENV_PYTHON, 'dashboard.py'],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=utf8_env,
    )
    threading.Thread(target=_stream, args=(dashboard, '[dashboard]'), daemon=True).start()

    # ── Signal engine — give dashboard a moment to bind port 8888 ─────────────
    time.sleep(3)
    engine = subprocess.Popen(
        [VENV_PYTHON, 'signal_engine.py'],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=utf8_env,
    )
    threading.Thread(target=_stream, args=(engine, '[engine]  '), daemon=True).start()

    # ── Discord OCR source — the ticker source (replaces audio transcription) ──
    discord = subprocess.Popen(
        [VENV_PYTHON, 'discord_source.py'],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=utf8_env,
    )
    threading.Thread(target=_stream, args=(discord, '[discord] '), daemon=True).start()

    procs = {'dashboard': dashboard, 'engine': engine, 'discord': discord}

    print('Dashboard     ->  http://localhost:8888')
    print('Signal engine ->  running (logs prefixed [engine])')
    print('Discord OCR   ->  running (logs prefixed [discord])')
    print('Press Ctrl+C to stop all.\n')

    try:
        while True:
            for name, proc in procs.items():
                if proc.poll() is not None:
                    print(f'\n[{name}] exited with code {proc.returncode}')
                    raise _ProcExited
            threading.Event().wait(1)
    except (KeyboardInterrupt, _ProcExited):
        print('\nStopping...')
    finally:
        ordered = [p for n, p in procs.items() if n != 'dashboard'] + [dashboard]
        for proc in ordered:
            if proc.poll() is None:
                proc.terminate()
        for proc in ordered:
            try:
                proc.wait(timeout=5)
            except (KeyboardInterrupt, subprocess.TimeoutExpired):
                proc.kill()
                proc.wait()


if __name__ == '__main__':
    main()
