#!/usr/bin/env python3
"""
check_js_versions.py — Guard against ES-module cache-bust version desync.

Every static JS module is imported with a `?v=N` query string so the browser
re-fetches it after a change. If two files import the *same* module with
*different* `?v=` values, the browser loads two separate instances of that
module — each with its own private state. That silently broke the Stop
Transcription button once (controls.js imported store.js?v=40 while everyone
else used ?v=38, so the click handler read a stale, never-updated store).

This script enforces a single global `?v=` value across:
  • every `import ... from './x.js?v=N'` inside static/js/*.js
  • the `<script src="/static/js/app.js?v=N">` entry point in dashboard.html

Exits non-zero (and prints the offenders) on any mismatch, so the desync is
caught at commit time instead of in the browser.
"""

import re
import sys
from collections import Counter
from pathlib import Path

ROOT    = Path(__file__).resolve().parent.parent
JS_DIR  = ROOT / "static" / "js"
HTML    = ROOT / "dashboard.html"

# Matches  ?v=42  in both JS imports and the HTML entry script.
_V_RE = re.compile(r"\?v=(\d+)")


def collect() -> list[tuple[str, int]]:
    """Return (location, version) for every ?v= occurrence we care about."""
    found: list[tuple[str, int]] = []

    for js in sorted(JS_DIR.glob("*.js")):
        for i, line in enumerate(js.read_text(encoding="utf-8").splitlines(), 1):
            for m in _V_RE.finditer(line):
                found.append((f"{js.relative_to(ROOT)}:{i}", int(m.group(1))))

    if HTML.exists():
        for i, line in enumerate(HTML.read_text(encoding="utf-8").splitlines(), 1):
            if "/static/js/" in line:
                for m in _V_RE.finditer(line):
                    found.append((f"{HTML.relative_to(ROOT)}:{i}", int(m.group(1))))

    return found


def main() -> int:
    found = collect()
    if not found:
        print("check_js_versions: no ?v= versions found — nothing to check.")
        return 0

    versions = Counter(v for _, v in found)
    if len(versions) == 1:
        only = next(iter(versions))
        print(f"check_js_versions: OK — all {len(found)} imports at ?v={only}.")
        return 0

    # Desync: report the minority (most-likely-wrong) locations first.
    majority, _ = versions.most_common(1)[0]
    print("check_js_versions: ERROR — mismatched ?v= cache-bust versions.\n", file=sys.stderr)
    print(f"  Most common is ?v={majority} ({versions[majority]} imports). Offenders:", file=sys.stderr)
    for loc, ver in found:
        if ver != majority:
            print(f"    {loc}  →  ?v={ver}", file=sys.stderr)
    print("\n  Fix: set every ?v= to one shared value, then bump it on the next change.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
