#!/usr/bin/env python3
"""watchdog.py — keep the desk's long-running processes up, and the pidfile honest.

Nothing supervised the stack before this. `./trading start` launches each
process with `nohup … &`, records the PID, and exits; `start_all.py` does the
opposite and tears the *whole* stack down the moment any one child exits. So a
dashboard that died mid-session stayed dead until someone noticed the feed had
gone quiet — which is exactly how it failed: the Discord OCR source kept
capturing alerts and POSTing them into a refused connection, losing every
ticker it found, while the engine and the OCR source both looked healthy.

What it does, every `--interval` seconds:

  • Prunes dead PIDs from .trading.pids and records the ones it starts, so
    `./trading stop` can still find everything.
  • Health-checks each service and restarts what is down, with exponential
    backoff so a process that crashes on startup cannot hot-loop.
  • After the AI watch opens, runs tools/instrumentation_check.py about once
    an hour so a silent logger is a same-day alarm, not a next-morning autopsy.
  • After the cash session (default 16:05 ET), runs tools/daily_learn.py once
    so the hybrid forward-test ledger always gets a line. daily_learn then
    runs tools/replay_ab.py on that day's tape and records the overlay ranking.

The dashboard is checked over HTTP rather than by process liveness: a hung
uvicorn still has a PID, and to the OCR source a hang and a crash are the same
outage. Everything else is checked by process liveness — they have no port.

Deliberate stops are not fought. cmd_stop in `./trading` kills the watchdog
first (it is tracked in .trading.pids like everything else) and removes the
pidfile, so there is no window where it resurrects what you just stopped.

Run:
    venv/bin/python tools/watchdog.py                 # foreground, logs to stdout
    ./trading start                                   # started for you
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT     = Path(__file__).resolve().parent.parent
PIDFILE  = ROOT / ".trading.pids"
SELF_PID = ROOT / ".trading.watchdog.pid"
LOGDIR   = ROOT / "logs"
ET       = ZoneInfo("America/New_York")

# Backoff bounds for a service that will not stay up. Capped rather than
# abandoned: an unattended desk is worth more with a process that retries every
# two minutes than one that gave up at 09:31 and told nobody.
BACKOFF_BASE_SEC = 5.0
BACKOFF_CAP_SEC  = 120.0

# Learning-loop schedules (ET). Pure helpers below are unit-tested.
DEFAULT_INSTR_START = "09:00"   # match ai_watch_start_time when config missing
DEFAULT_EOD_HHMM = "16:05"      # after cash close; outcomes settled


def log(msg: str) -> None:
    print(f"[watchdog] {time.strftime('%H:%M:%S')} {msg}", flush=True)


# ── Pure helpers (unit-tested; no processes, no clock) ───────────────────────

def restart_delay(failures: int,
                  base: float = BACKOFF_BASE_SEC,
                  cap: float = BACKOFF_CAP_SEC) -> float:
    """Seconds to wait before the next restart attempt.

    `failures` counts *consecutive* failed starts; it resets as soon as a
    service is seen healthy. The first restart is immediate — the common case
    is a one-off death, and making the desk wait on that would be the very
    delay this exists to remove.
    """
    if failures <= 0:
        return 0.0
    return min(base * (2 ** (failures - 1)), cap)


def prune_pids(pids: list[int], alive) -> list[int]:
    """Drop PIDs that are no longer running, preserving order and de-duping.

    A stale pidfile is not merely untidy: PIDs are recycled, so `./trading stop`
    reading an old entry can signal an unrelated process that happens to have
    inherited the number.
    """
    seen: set[int] = set()
    kept: list[int] = []
    for pid in pids:
        if pid in seen or not alive(pid):
            continue
        seen.add(pid)
        kept.append(pid)
    return kept


def read_pidfile(path: Path) -> list[int]:
    try:
        return [int(ln.strip()) for ln in path.read_text().split() if ln.strip()]
    except (OSError, ValueError):
        return []


def write_pidfile(path: Path, pids: list[int]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(str(p) for p in pids) + ("\n" if pids else ""))
    os.replace(tmp, path)


def pid_alive(pid: int) -> bool:
    """Whether `pid` names a live process. Signal 0 checks without delivering."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # exists, owned by someone else
    return True


# ── Services ─────────────────────────────────────────────────────────────────

@dataclass
class Service:
    name:      str
    script:    str                     # path relative to ROOT; default pgrep pattern
    logfile:   str
    health_url: str | None = None      # when set, HTTP decides health, not liveness
    argv:      list[str] | None = None  # full command; None → [py, "-u", script]
    pattern:   str | None = None        # pgrep pattern when it differs from script
    want_file: Path | None = None       # when set, supervise only while it exists
    failures:  int = 0                 # consecutive failed starts
    next_try:  float = 0.0             # epoch before which we do not retry
    pid:       int | None = None

    def wanted(self) -> bool:
        """Whether this service is supposed to be up at all.

        Only the tunnel uses this. `./trading start local` deliberately runs
        without it, and `./trading tunnel stop` deliberately takes it down —
        a supervisor that cannot tell "down" from "switched off" would undo
        both. The marker is written and removed by those commands, and it is
        re-read every cycle so turning the tunnel on later needs no restart.
        """
        return self.want_file is None or self.want_file.exists()

    def command(self, py: str) -> list[str]:
        return self.argv if self.argv else [py, "-u", self.script]

    def healthy(self, py: str) -> bool:
        if self.health_url:
            try:
                with urllib.request.urlopen(self.health_url, timeout=3) as r:
                    return 200 <= r.status < 400
            except (urllib.error.URLError, OSError, ValueError):
                return False
        return process_running(self.pattern or self.script)

    def start(self, py: str) -> int | None:
        LOGDIR.mkdir(exist_ok=True)
        try:
            fh = open(LOGDIR / self.logfile, "a")
        except OSError as e:
            log(f"{self.name}: cannot open log — {e}")
            return None
        try:
            # start_new_session detaches the child into its own process group.
            # Without it the child dies with whatever shell or task launched the
            # watchdog, which is its own flavour of the bug this file exists for.
            proc = subprocess.Popen(
                self.command(py),
                cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as e:
            log(f"{self.name}: start failed — {e}")
            return None
        finally:
            fh.close()
        return proc.pid


def process_running(pattern: str) -> bool:
    """True when any process command line matches `pattern` (pgrep -f)."""
    try:
        return subprocess.run(
            ["pgrep", "-f", pattern],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0
    except OSError:
        return False


def default_services(port: int) -> list[Service]:
    """The processes whose death silently costs data.

    The dashboard is first because everything else feeds it — when it is down
    the OCR source's captures go nowhere. Producers that only write files on a
    schedule (swing, rs) are left out on purpose: missing one run is visible in
    its own output, and restarting them has no urgency.
    """
    return [
        Service("dashboard", "dashboard.py", "dashboard.log",
                health_url=f"http://localhost:{port}/api/meta"),
        Service("engine", "signal_engine.py", "engine.log"),
        Service("discord", "discord_source.py", "discord.log"),
    ]


# Where `./trading` looks for cloudflared, in its order of preference.
CLOUDFLARED_PATHS = ("/opt/homebrew/bin/cloudflared", "/usr/local/bin/cloudflared")
TUNNEL_NAME       = "trading-helper"
TUNNEL_WANT_FILE  = ROOT / ".trading.tunnel.want"


def find_cloudflared(paths=CLOUDFLARED_PATHS) -> str | None:
    for p in paths:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def tunnel_service(binary: str) -> Service:
    """The Cloudflare tunnel that publishes the dashboard at its public name.

    Matched by the same `cloudflared.*<tunnel>` pattern `./trading` uses, not by
    a script path — it is not a Python process and has no port of ours to poll.
    Its own log keeps the tunnel's connection detail where you already look.
    """
    return Service(
        name="tunnel",
        script=binary,
        logfile="tunnel.log",
        argv=[binary, "tunnel", "--config",
              str(ROOT / "config" / "cloudflared-config.yml"),
              "run", TUNNEL_NAME],
        pattern=f"cloudflared.*{TUNNEL_NAME}",
        want_file=TUNNEL_WANT_FILE,
    )


_AUTO = object()   # "look it up" — distinct from an explicit None ("no binary")


def enabled_services(cfg: dict, port: int, cloudflared=_AUTO) -> list[Service]:
    """Drop services their config flag has switched off, so a deliberately
    disabled producer is not restarted into existence every interval.

    The tunnel is gated twice: the binary has to exist at all, and `wanted()`
    re-checks the marker each cycle (see Service.wanted).
    """
    out = []
    for svc in default_services(port):
        if svc.name == "discord" and not cfg.get("discord_ocr_enabled", False):
            continue
        out.append(svc)
    binary = find_cloudflared() if cloudflared is _AUTO else cloudflared
    if binary:
        out.append(tunnel_service(binary))
    return out


def load_cfg() -> dict:
    try:
        with open(ROOT / "config" / "bot_config.json") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _parse_hhmm(raw: str, default: str) -> dtime:
    s = (raw or default).strip() or default
    try:
        hh, mm = s.split(":", 1)
        return dtime(int(hh), int(mm))
    except Exception:
        hh, mm = default.split(":")
        return dtime(int(hh), int(mm))


def should_run_instrumentation(
    now_et: datetime,
    *,
    start_hhmm: str,
    last_key: str | None,
) -> tuple[bool, str]:
    """Once per ET hour after the desk is expected to record.

    Returns (run?, key) where key is YYYY-MM-DDTHH for dedupe.
    """
    start = _parse_hhmm(start_hhmm, DEFAULT_INSTR_START)
    if now_et.timetz().replace(tzinfo=None) < start:
        return False, last_key or ""
    key = now_et.strftime("%Y-%m-%dT%H")
    if last_key == key:
        return False, key
    return True, key


def should_run_eod(
    now_et: datetime,
    *,
    eod_hhmm: str,
    last_day: str | None,
) -> tuple[bool, str]:
    """Once per ET calendar day after *eod_hhmm*."""
    eod = _parse_hhmm(eod_hhmm, DEFAULT_EOD_HHMM)
    day = now_et.strftime("%Y-%m-%d")
    if now_et.timetz().replace(tzinfo=None) < eod:
        return False, last_day or ""
    if last_day == day:
        return False, day
    return True, day


def run_learn_job(py: str, *argv: str) -> int:
    """Run a tools/*.py job; never raise into the supervisor loop.

    Stdout used to go to DEVNULL, so EOD thesis/h4/eod results vanished
    and a nonzero rc was a mystery (instrumentation_check rc=1 all of
    2026-08-21). Append to logs/learn.log.
    """
    script = ROOT / "tools" / argv[0]
    cmd = [py, str(script), *argv[1:]]
    try:
        LOGDIR.mkdir(parents=True, exist_ok=True)
        learn_log = LOGDIR / "learn.log"
        with open(learn_log, "a", encoding="utf-8") as fh:
            fh.write(f"\n===== {' '.join(argv)} {datetime.now(tz=ET)} =====\n")
            fh.flush()
            return subprocess.call(cmd, cwd=str(ROOT), stdout=fh, stderr=fh)
    except OSError as e:
        log(f"learn job failed to spawn: {e}")
        return 127


# ── Main loop ────────────────────────────────────────────────────────────────

def sync_pidfile(extra: list[int]) -> None:
    """Prune the dead and record `extra`, leaving the file honest either way."""
    pids = prune_pids(read_pidfile(PIDFILE) + extra, pid_alive)
    write_pidfile(PIDFILE, pids)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--interval", type=float, default=10.0,
                    help="seconds between health checks (default 10)")
    ap.add_argument("--port", type=int, default=8888,
                    help="dashboard port for the health check (default 8888)")
    ap.add_argument("--once", action="store_true",
                    help="run a single check and exit (for testing)")
    ap.add_argument("--no-learn", action="store_true",
                    help="skip instrumentation / EOD daily_learn jobs")
    ap.add_argument("--eod-time", default=DEFAULT_EOD_HHMM,
                    help="ET HH:MM for daily_learn (default 16:05)")
    args = ap.parse_args()

    py = str(ROOT / ".venv" / "bin" / "python")
    if not os.path.isfile(py):
        py = sys.executable

    # Refuse to double-run: two watchdogs race to restart the same corpse and
    # leave two dashboards fighting over the port.
    if SELF_PID.exists():
        prev = read_pidfile(SELF_PID)
        if prev and pid_alive(prev[0]) and prev[0] != os.getpid():
            log(f"already running as pid {prev[0]} — exiting")
            return 0
    write_pidfile(SELF_PID, [os.getpid()])
    sync_pidfile([os.getpid()])

    cfg = load_cfg()
    services = enabled_services(cfg, args.port)
    watch_start = str(cfg.get("ai_watch_start_time") or DEFAULT_INSTR_START)
    log(f"supervising {', '.join(s.name for s in services)} "
        f"every {args.interval:g}s"
        + ("" if args.no_learn else
           f"; learn: instr≥{watch_start} eod≥{args.eod_time} ET"))

    last_instr_key: str | None = None
    last_eod_day: str | None = None
    stopping = False

    def _stop(_sig, _frm):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    try:
        while not stopping:
            now = time.time()
            started: list[int] = []

            for svc in services:
                if not svc.wanted():
                    svc.failures = 0
                    continue

                if svc.healthy(py):
                    if svc.failures:
                        log(f"{svc.name}: healthy again after {svc.failures} "
                            f"failed start(s)")
                    svc.failures = 0
                    continue

                if now < svc.next_try:
                    continue

                pid = svc.start(py)
                if pid is None:
                    svc.failures += 1
                    svc.next_try = now + restart_delay(svc.failures)
                    continue

                svc.pid = pid
                started.append(pid)
                svc.failures += 1          # cleared on the next healthy check
                svc.next_try = now + restart_delay(svc.failures)
                log(f"{svc.name}: was down — restarted as pid {pid} "
                    f"(attempt {svc.failures})")

            sync_pidfile(started)

            if not args.no_learn:
                now_et = datetime.now(tz=ET)
                run_i, last_instr_key = should_run_instrumentation(
                    now_et, start_hhmm=watch_start, last_key=last_instr_key)
                if run_i:
                    rc = run_learn_job(py, "instrumentation_check.py")
                    log(f"instrumentation_check rc={rc} key={last_instr_key}")
                    if rc != 0:
                        log("instrumentation_check NONZERO — logging may be "
                            "SILENT after desk start; check ai_reports/")

                run_e, day_key = should_run_eod(
                    now_et, eod_hhmm=args.eod_time, last_day=last_eod_day)
                if run_e:
                    rc = run_learn_job(py, "daily_learn.py", "--day", day_key)
                    last_eod_day = day_key
                    log(f"daily_learn rc={rc} day={day_key}")
                    rc_e = run_learn_job(py, "eod.py", "--days", "10")
                    rc_t = run_learn_job(
                        py, "thesis_screen.py",
                        "--days", "1", "--horizon-min", "60",
                        "--slices", "late", "--flatten-et", "15:50",
                    )
                    rc_h = run_learn_job(py, "h4_screen.py", "--days", "20")
                    log(f"eod rc={rc_e}; thesis_late rc={rc_t}; h4_screen rc={rc_h}")
                    # Freeze the last 10 sessions and rank the declared
                    # settings grid. Incremental jsonl so a killed run
                    # still leaves a morning brief. Must not write config.
                    rc_p = run_learn_job(py, "desk_tape.py", "pack", "--days", "10")
                    rc_s = run_learn_job(
                        py, "replay_ab.py", "--search", "--days", "10")
                    log(f"desk_tape pack rc={rc_p}; replay_ab --search rc={rc_s}")

            if args.once:
                break
            time.sleep(args.interval)
    finally:
        try:
            SELF_PID.unlink()
        except OSError:
            pass
        log("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
