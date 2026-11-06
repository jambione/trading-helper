"""Feed-regime split for the forward readers (operator 11/06 'yes' to the SIP amendment).

The logged range / efficiency / run-size / slope measures come from the in-memory 1-minute bar store: IEX bars until the SIP flip,
SIP bars after. Dense SIP bars change what 'choppy' or 'top of range' reads as, so the two periods are never pooled. Each reader
reads ONCE, on the first feed sample whose entry count reaches the prereg's minimum (ordered by entry time); the other sample is
information only. docs/studies/feed_regime.json holds the flip time (null = all IEX)."""
from __future__ import annotations

import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ET = ZoneInfo("America/New_York")
PATH = os.path.join(ROOT, "docs", "studies", "feed_regime.json")


def sip_from_ts(path: str | None = None):
    try:
        v = json.load(open(path or PATH)).get("sip_from_et")
    except (OSError, ValueError):
        return None
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v))
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=ET)).timestamp()


def split(rows, sip_from=None):
    cut = sip_from if sip_from is not None else sip_from_ts()
    iex = [r for r in rows if cut is None or r["t"] < cut]
    sip = [r for r in rows if cut is not None and r["t"] >= cut]
    return {"IEX": iex, "SIP": sip}


def pick(rows, min_n, sip_from=None):
    """-> (label or None, sample or None, info lines). Counts only: no outcome numbers are printed here."""
    s = split(rows, sip_from)
    when = {}
    for lab, rs in s.items():
        ts = sorted(r["t"] for r in rs)
        if len(ts) >= min_n:
            when[lab] = ts[min_n - 1]
    info = [f"feed regime: IEX sample {len(s['IEX'])} entries, SIP sample {len(s['SIP'])} entries (each read separately, never pooled)"]
    if not when:
        return None, None, info
    lab = min(when, key=when.get)
    info.append(f"read on the {lab} sample (first to reach {min_n} entries); the other sample is information only")
    return lab, s[lab], info
