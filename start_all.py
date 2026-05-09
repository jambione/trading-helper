#!/usr/bin/env python3
"""Start the Signal Scanner dashboard.

Scanner engine is now embedded — no separate process needed.
"""
import os
import subprocess
import sys
import threading

ROOT = os.path.dirname(os.path.abspath(__file__))


def _stream(proc: subprocess.Popen, label: str) -> None:
    assert proc.stdout
    for raw in iter(proc.stdout.readline, b''):
        sys.stdout.write(f'{label} {raw.decode(errors="replace")}')
        sys.stdout.flush()


def main() -> None:
    utf8_env = {**os.environ, 'PYTHONUTF8': '1'}

    helper = subprocess.Popen(
        [sys.executable, 'dashboard.py'],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=utf8_env,
    )

    threading.Thread(target=_stream, args=(helper, '[dashboard]'), daemon=True).start()

    print('Signal Scanner  ->  http://localhost:8888')
    print('Press Ctrl+C to stop.\n')

    try:
        while True:
            if helper.poll() is not None:
                print(f'\n[dashboard] exited with code {helper.returncode}')
                break
            threading.Event().wait(1)
    except KeyboardInterrupt:
        print('\nStopping...')
    finally:
        if helper.poll() is None:
            helper.terminate()
        try:
            helper.wait(timeout=5)
        except (KeyboardInterrupt, subprocess.TimeoutExpired):
            helper.kill()
            helper.wait()


if __name__ == '__main__':
    main()
