#!/usr/bin/env python3
"""Start trading-helper + trading-scanners backend together.

Streams interleaved output from both processes with prefixed labels.
Ctrl-C (or either process dying) shuts everything down cleanly.
"""
import os
import subprocess
import sys
import threading

ROOT       = os.path.dirname(os.path.abspath(__file__))
SCANNER_BE = os.path.normpath(os.path.join(ROOT, '..', 'trading-scanners', 'backend'))


def _stream(proc: subprocess.Popen, label: str) -> None:
    assert proc.stdout
    for raw in iter(proc.stdout.readline, b''):
        sys.stdout.write(f'{label} {raw.decode(errors="replace")}')
        sys.stdout.flush()


def _check_env() -> None:
    env_path = os.path.join(SCANNER_BE, '.env')
    if not os.path.exists(env_path):
        print(f'[warn] {env_path} not found — copy backend/.env.example and add your Alpaca keys.')


def main() -> None:
    _check_env()

    # Force UTF-8 I/O so emoji in print() don't crash under cp1252 piped stdout
    utf8_env = {**os.environ, 'PYTHONUTF8': '1'}

    scanner = subprocess.Popen(
        [sys.executable, '-m', 'uvicorn', 'main:app', '--port', '8000'],
        cwd=SCANNER_BE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=utf8_env,
    )
    helper = subprocess.Popen(
        [sys.executable, 'dashboard.py'],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=utf8_env,
    )

    threading.Thread(target=_stream, args=(scanner, '[scanner]'), daemon=True).start()
    threading.Thread(target=_stream, args=(helper,  '[helper] '), daemon=True).start()

    print('Signal Scanner  ->  http://localhost:8888')
    print('Scanner backend ->  http://localhost:8000')
    print('Press Ctrl+C to stop.\n')

    try:
        while True:
            if scanner.poll() is not None:
                print(f'\n[scanner] exited with code {scanner.returncode}')
                helper.terminate()
                break
            if helper.poll() is not None:
                print(f'\n[helper] exited with code {helper.returncode}')
                scanner.terminate()
                break
            threading.Event().wait(1)
    except KeyboardInterrupt:
        print('\nStopping...')
    finally:
        for proc in (scanner, helper):
            if proc.poll() is None:
                proc.terminate()
        scanner.wait()
        helper.wait()


if __name__ == '__main__':
    main()
