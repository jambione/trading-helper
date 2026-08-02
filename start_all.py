#!/usr/bin/env python3
"""Start the Signal Scanner dashboard + signal engine.

Both processes run together; Ctrl+C or either process exiting stops both.
Uses venv when available, otherwise falls back to the system python3.
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

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
    if proc.stdout is None:
        raise RuntimeError(f"[{label}] subprocess stdout is None — cannot stream output")

    for raw in iter(proc.stdout.readline, b''):
        sys.stdout.write(f'{label} {raw.decode(errors="replace")}')
        sys.stdout.flush()


def _load_cfg() -> dict:
    try:
        cfg_path = os.path.join(ROOT, 'config', 'bot_config.json')
        with open(cfg_path) as f:
            return json.load(f)
    except Exception:
        return {}


def main() -> None:
    cfg = _load_cfg()

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

    # ── Signal engine — wait for dashboard to accept connections ─────────────
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            urllib.request.urlopen("http://localhost:8888/api/meta", timeout=1)
            break
        except Exception:
            time.sleep(0.25)
    engine = subprocess.Popen(
        [VENV_PYTHON, 'signal_engine.py'],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=utf8_env,
    )
    threading.Thread(target=_stream, args=(engine, '[engine]  '), daemon=True).start()

    procs = {'dashboard': dashboard, 'engine': engine}

    print('Dashboard     ->  http://localhost:8888')
    print('Signal engine ->  running (logs prefixed [engine])')

    # ── Discord OCR source — only launched when enabled in config ─────────────
    if cfg.get('discord_ocr_enabled', False):
        discord = subprocess.Popen(
            [VENV_PYTHON, 'discord_source.py'],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=utf8_env,
        )
        threading.Thread(target=_stream, args=(discord, '[discord] '), daemon=True).start()
        procs['discord'] = discord
        print('Discord OCR   ->  running (logs prefixed [discord])')
    else:
        print('Discord OCR   ->  disabled (set discord_ocr_enabled: true in config/bot_config.json)')

    # ── Swing screener — only launched when enabled in config ─────────────────
    if cfg.get('swing_screener_enabled', False):
        swing = subprocess.Popen(
            [VENV_PYTHON, 'swing_screener.py'],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=utf8_env,
        )
        threading.Thread(target=_stream, args=(swing, '[swing]   '), daemon=True).start()
        procs['swing'] = swing
        print('Swing screener->  running (scheduled runs; logs prefixed [swing])')
    else:
        print('Swing screener->  disabled (set swing_screener_enabled: true in config/bot_config.json)')

    # ── RS screener — only launched when enabled in config ────────────────────
    if cfg.get('rs_screener_enabled', False):
        rs = subprocess.Popen(
            [VENV_PYTHON, 'rs_screener.py'],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=utf8_env,
        )
        threading.Thread(target=_stream, args=(rs, '[rs]      '), daemon=True).start()
        procs['rs'] = rs
        print('RS screener   ->  running (one run/day after the close; logs prefixed [rs])')
    else:
        print('RS screener   ->  disabled (set rs_screener_enabled: true in config/bot_config.json)')

    # ── Claude trader — only launched when enabled in config ──────────────────
    # This is the only process here that can place broker orders. Before
    # enabling it, confirm the desk monitor's momentum_config.json has
    # claude_trading_enabled: false — two processes managing the same account
    # would double every entry.
    if cfg.get('claude_trader_enabled', False):
        claude = subprocess.Popen(
            [VENV_PYTHON, 'claude_trader.py'],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=utf8_env,
        )
        threading.Thread(target=_stream, args=(claude, '[claude]  '), daemon=True).start()
        procs['claude'] = claude
        print('Claude trader ->  running (scheduled research + entries; logs prefixed [claude])')
    else:
        print('Claude trader ->  disabled (set claude_trader_enabled: true in config/bot_config.json)')

    # ── Trending screener — only launched when enabled in config ──────────────
    if cfg.get('trending_screener_enabled', False):
        trending = subprocess.Popen(
            [VENV_PYTHON, 'trending_screener.py'],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=utf8_env,
        )
        threading.Thread(target=_stream, args=(trending, '[trending]'), daemon=True).start()
        procs['trending'] = trending
        print('Trending      ->  running (Stocktwits poll; logs prefixed [trending])')
    else:
        print('Trending      ->  disabled (set trending_screener_enabled: true in config/bot_config.json)')

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
