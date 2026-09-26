"""desk_io — the desk's one wire boundary: record every external read live,
serve exactly those reads in a replay.

Why this exists. On 2026-09-25 a replay of the code that actually ran matched
1 of 13 live buys. Every divergence traced back to the same pattern: the desk
read something that was never recorded, and the replay quietly substituted a
guess — a float cache it could not see (so the float gate failed open), an
empty bar cache (so the range gate failed open), a price clock rebuilt from
aged dashboard rows (so stale tape looked fresh). Patching those one at a time
fixes the ones we found. This module removes the class: every byte the desk
process reads from outside itself crosses one of the channels below, is
recorded as it arrives, and a replay serves the recorded bytes — never a
fetch, never a guess. A read the recording cannot serve is a MISS, counted by
channel and caller, so an incomplete recording is loud instead of plausible.

Channels
  alpaca   every alpaca-py REST call (market data and trading reads), patched
           at RESTClient._request: method, path, params in; JSON out.
  dash     GET /api/state from the dashboard (price stream, engine %R, source
           panels). ~115 KB, fetched up to 4x/s, so it is delta-encoded: only
           rows that changed are written, and every "*_age_sec" is stored as
           the epoch it implies, so a row changes only when a new print does.

  file     every *.json file under the repo that this process reads but did
           not write (panels, research boards, engine state, float cache...),
           caught at builtins.open / os.stat so no read site can be missed.
           The bytes recorded are the bytes handed to the caller; a document
           is written (delta-encoded) only when it changed.

Stream: ai_reports/sessions/<day>/wire.jsonl.gz (session_recorder).

Modes: "off" (default, nothing patched), "live" (record), "replay" (serve).
A replay miss raises ReplayMiss, which callers see as the network failure it
stands in for; their existing failure paths then run, and the miss is counted.
"""
from __future__ import annotations

import bisect
import builtins
import gzip
import hashlib
import io
import json
import os
import stat as _stat
import sys
import threading
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

MODE = "off"
_lock = threading.Lock()
_orig_request: Callable | None = None
_clock: Callable[[], float] = time.time

# Replay bookkeeping: what was served, what was missed and who asked.
served: Counter = Counter()
missed: Counter = Counter()
miss_callers: Counter = Counter()


class ReplayMiss(Exception):
    """A read the recording cannot serve. Stands in for a network failure."""


# ── encoding helpers ───────────────────────────────────────────────────────

def _jsonable(v: Any) -> Any:
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if hasattr(v, "value") and not isinstance(v, (str, int, float, bool)):
        return _jsonable(v.value)  # enums (DataFeed.IEX -> "iex")
    return v


# Request params that only say WHEN the request was made. A replay asks at its
# own clock, so these are left out of the lookup key and the response live got
# at the latest moment on or before the replay's clock is served instead.
_TIME_PARAMS = {"start", "end", "asof", "after", "until", "date"}


def alpaca_key(method: str, path: str, params: Any) -> str:
    p = params if isinstance(params, dict) else {}
    keep = {k: p[k] for k in sorted(p) if k not in _TIME_PARAMS and p[k] is not None}
    return f"{str(method).upper()} {path} {json.dumps(_jsonable(keep), sort_keys=True)}"


def _caller() -> str:
    """First frame outside this module and alpaca-py: who asked."""
    f = sys._getframe(2)
    while f is not None:
        mod = f.f_globals.get("__name__", "")
        if mod != __name__ and not mod.startswith("alpaca"):
            return f"{mod}.{f.f_code.co_name}"
        f = f.f_back
    return "?"


def _record(obj: dict) -> None:
    try:
        import session_recorder
        session_recorder._append("wire", obj)
    except Exception:  # noqa: BLE001 — recording must never break a read
        pass


# ── dashboard payload delta codec ──────────────────────────────────────────

def _to_epochs(v: Any, t: float) -> Any:
    """Every '*_age_sec' number becomes '*_age_sec@' = the epoch it implies."""
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            if isinstance(k, str) and k.endswith("_age_sec") and isinstance(x, (int, float)) \
                    and not isinstance(x, bool):
                out[k + "@"] = round(t - float(x), 3)
            else:
                out[k] = _to_epochs(x, t)
        return out
    if isinstance(v, list):
        return [_to_epochs(x, t) for x in v]
    return v


def _from_epochs(v: Any, t: float) -> Any:
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            if isinstance(k, str) and k.endswith("_age_sec@"):
                out[k[:-1]] = round(max(0.0, t - float(x)), 3)
            else:
                out[k] = _from_epochs(x, t)
        return out
    if isinstance(v, list):
        return [_from_epochs(x, t) for x in v]
    return v


def _row_id(row: Any) -> str | None:
    if isinstance(row, dict):
        for k in ("ticker", "symbol"):
            if row.get(k):
                return str(row[k]).upper()
    return None


def _digest(v: Any) -> str:
    return hashlib.blake2b(json.dumps(v, sort_keys=True, default=str).encode(),
                           digest_size=8).hexdigest()


_MAX_DEPTH = 4


def _shape(v: Any, depth: int) -> str:
    """How a value is diffed: 'rows' (list of ticker/symbol rows with unique
    ids), 'map' (dict of dicts, items whole), 'obj' (other dict, per key,
    recursively) or 'leaf' (written whole when its digest changes)."""
    if depth >= _MAX_DEPTH:
        return "leaf"
    if isinstance(v, list) and v:
        ids = [_row_id(r) for r in v]
        if all(ids) and len(set(ids)) == len(ids):
            return "rows"
    if isinstance(v, dict) and v:
        if all(isinstance(x, dict) for x in v.values()):
            return "map"
        return "obj"
    return "leaf"


def _enc(prev: Any, v: Any, depth: int) -> tuple[Any, Any]:
    """(delta or None, new state). State: ('leaf', dig) | ('obj', {k: state})
    | (kind, ids, {id: dig}) for rows/map."""
    shape = _shape(v, depth)
    if shape == "leaf":
        d = _digest(v)
        if prev is not None and prev[0] == "leaf" and prev[1] == d:
            return None, prev
        return {"w": v}, ("leaf", d)
    if shape == "obj":
        kids = prev[1] if prev is not None and prev[0] == "obj" else None
        full = kids is None
        kids = {} if full else kids
        out, new = {}, {}
        for k, x in v.items():
            sub, st = _enc(kids.get(k), x, depth + 1)
            new[k] = st
            if sub is not None:
                out[k] = sub
        gone = [k for k in kids if k not in v]
        if not out and not gone and not full:
            return None, ("obj", new)
        delta: dict[str, Any] = {"o": out}
        if gone:
            delta["g"] = gone
        if full:
            delta["n"] = True
        return delta, ("obj", new)
    if shape == "rows":
        ids = [_row_id(r) for r in v]
        items = list(v)
    else:
        ids = [str(k) for k in v]
        items = list(v.values())
    same_kind = prev is not None and prev[0] == shape
    old = prev[2] if same_kind else {}
    cur, changed = {}, {}
    for rid, item in zip(ids, items):
        d = _digest(item)
        cur[rid] = d
        if old.get(rid) != d:
            changed[rid] = item
    delta = {}
    if changed:
        delta["i"] = changed
    if not same_kind or ids != prev[1]:
        delta["k"] = shape
        delta["ids"] = ids
    return (delta or None), (shape, ids, cur)


def _dec(node: Any, delta: dict) -> Any:
    """Apply a delta to a decoder node; returns the new node.
    Node: {'w': value} | {'o': {k: node}} | {'k': kind, 'ids': [...], 'i': {...}}."""
    if "w" in delta:
        return {"w": delta["w"]}
    if "o" in delta:
        kids = {} if delta.get("n") or node is None or "o" not in node else dict(node["o"])
        for k in delta.get("g") or []:
            kids.pop(k, None)
        for k, sub in delta["o"].items():
            kids[k] = _dec(kids.get(k), sub)
        return {"o": kids}
    if "k" in delta:
        same = node is not None and node.get("k") == delta["k"]
        items = dict(node["i"]) if same else {}
        keep = set(delta["ids"])
        items = {r: x for r, x in items.items() if r in keep}
        items.update(delta.get("i") or {})
        return {"k": delta["k"], "ids": list(delta["ids"]), "i": items}
    items = dict(node["i"])
    items.update(delta.get("i") or {})
    return {"k": node["k"], "ids": node["ids"], "i": items}


def _materialize(node: Any) -> Any:
    if node is None:
        return None
    if "w" in node:
        return node["w"]
    if "o" in node:
        return {k: _materialize(x) for k, x in node["o"].items()}
    items, ids = node["i"], node["ids"]
    if node["k"] == "rows":
        return [items[i] for i in ids if i in items]
    return {i: items[i] for i in ids if i in items}


class DeltaEncoder:
    """Turns successive JSON documents into small deltas.

    Dicts are diffed key by key (recursively, to depth 4); lists of
    ticker/symbol rows and dicts of dicts item by item, with the id order
    written only when it changes; anything else whole when it changes. Ages
    ('*_age_sec') are stored as the epochs they imply, so a row is rewritten
    when its data changes, not every time the clock ticks.
    """

    def __init__(self):
        self.state: Any = None
        self.fresh = True

    def encode(self, doc: Any, t: float) -> dict:
        norm = _to_epochs(doc, t)
        # A new encoder (process start) knows nothing of what was written
        # before it, so its first record is a full document the decoder must
        # take as a reset, not a delta on top of an older process's state.
        reset = self.fresh
        self.fresh = False
        delta, self.state = _enc(None if reset else self.state, norm, 0)
        out: dict[str, Any] = {}
        if delta is not None:
            out["d"] = delta
        if reset:
            out["reset"] = True
        return out


class DeltaDecoder:
    """Rebuilds each recorded document from its deltas."""

    def __init__(self):
        self.node: Any = None

    def apply(self, rec: dict) -> None:
        if rec.get("reset"):
            self.node = None
        if "d" in rec:
            self.node = _dec(self.node, rec["d"])

    def document(self, t: float) -> Any:
        return _from_epochs(_materialize(self.node), t)

    payload = document


# Back-compat names used by the dashboard channel and its tests.
DashEncoder, DashDecoder = DeltaEncoder, DeltaDecoder


_dash_enc = DashEncoder()

# What the desk reads from /api/state, and so what is recorded. The payload
# also echoes the desk's own state back (ai_positions / claude_positions,
# ~370 MB a day each) and the research boards (read by the desk as files), so
# recording it whole would be ~1.2 GB a day of mostly our own output. A
# replay serves a TrackedPayload that counts any read of a key outside this
# set as a miss, so a new consumer shows up in the replay report instead of
# silently reading nothing.
DASH_KEYS = ("tickers", "bb_live", "engine", "trending", "movers", "funnel", "price_spikes")
DASH_SUBKEYS = {"ai_positions": ("account",)}


def dash_slice(payload: dict) -> dict:
    out = {k: payload[k] for k in DASH_KEYS if k in payload}
    for k, subs in DASH_SUBKEYS.items():
        v = payload.get(k)
        if isinstance(v, dict):
            out[k] = {s_: v[s_] for s_ in subs if s_ in v}
    return out


class TrackedPayload(dict):
    """A served payload that reports reads of keys the recording left out."""

    def __init__(self, data: dict, allowed: tuple, where: str = "dash"):
        super().__init__(data)
        self._allowed = set(allowed)
        self._where = where

    def _check(self, k) -> None:
        if k not in self._allowed:
            missed[f"{self._where} key {k}"] += 1
            miss_callers[_caller()] += 1

    def get(self, k, default=None):
        self._check(k)
        return super().get(k, default)

    def __getitem__(self, k):
        self._check(k)
        return super().__getitem__(k)


def _tracked(payload: dict) -> TrackedPayload:
    data = dict(payload)
    for k, subs in DASH_SUBKEYS.items():
        if isinstance(data.get(k), dict):
            data[k] = TrackedPayload(data[k], subs, f"dash {k}")
    return TrackedPayload(data, DASH_KEYS + tuple(DASH_SUBKEYS), "dash")


def record_dash(payload: dict) -> None:
    """Live: called with each fresh /api/state payload the desk fetched."""
    if MODE != "live" or not isinstance(payload, dict):
        return
    t = time.time()
    try:
        with _lock:
            enc = _dash_enc.encode(dash_slice(payload), t)
        obj = {"ts": t, "ch": "dash"}
        if enc:
            obj.update(enc)
        _record(obj)
    except Exception:  # noqa: BLE001
        pass


# ── file channel ───────────────────────────────────────────────────────────

ROOT = Path(os.path.dirname(os.path.abspath(__file__)))
_orig_open = builtins.open
_orig_stat = os.stat
_orig_replace = os.replace
_orig_rename = os.rename
_file_on = False
_own: set[str] = set()          # files this process wrote: its own state
_file_enc: dict[str, DeltaEncoder] = {}
_file_mt: dict[str, float] = {}
# Config is its own channel (session_recorder config stream) and secrets are
# never recorded; the recorder's own output is not an input.
_FILE_SKIP = ("config/", "ai_reports/sessions/", "tests/", ".venv/")


def _rel(path: Any) -> str | None:
    """Repo-relative path of an input JSON file, or None if not ours to catch."""
    try:
        if isinstance(path, int):
            return None
        p = os.fspath(path)
        if isinstance(p, bytes):
            p = p.decode()
    except TypeError:
        return None
    if not p.endswith(".json"):
        return None
    ap = os.path.abspath(p)
    # The report dir (AI_REPORT_DIR, a temp dir in a replay) is 'ai_reports/'
    # wherever it lives, so a file another process writes there has one name
    # live and in replay.
    # The more specific (longer) of the two prefixes wins.
    root = str(ROOT) + os.sep
    best = (len(root), "") if ap.startswith(root) else None
    rep = os.environ.get("AI_REPORT_DIR")
    if rep:
        rp = os.path.abspath(rep) + os.sep
        if ap.startswith(rp) and (best is None or len(rp) > best[0]):
            best = (len(rp), "ai_reports/")
    if best is None:
        return None
    return _skip(best[1] + ap[best[0]:])


def _skip(rel: str) -> str | None:
    if rel.startswith(_FILE_SKIP):
        return None
    return rel


def _is_read(mode: str) -> bool:
    return not any(c in mode for c in "wax+")


def _note_write(path: Any) -> None:
    r = _rel(path)
    if r is not None:
        _own.add(r)


def _record_file(rel: str, data: bytes, mtime: float) -> None:
    t = time.time()
    obj: dict[str, Any] = {"ts": t, "ch": "file", "f": rel, "mt": mtime}
    try:
        doc = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        doc = None
        obj["raw"] = data.decode("utf-8", errors="replace")
    with _lock:
        enc = _file_enc.setdefault(rel, DeltaEncoder())
        if doc is not None:
            d = enc.encode(doc, t)
            if not d and _file_mt.get(rel) == mtime:
                return
            obj.update(d)
        else:
            enc.fresh = True  # next parsable version must be a full reset
        _file_mt[rel] = mtime
    _record(obj)


def _live_open(file, mode="r", *args, **kwargs):
    if not _is_read(mode):
        _note_write(file)
        return _orig_open(file, mode, *args, **kwargs)
    rel = _rel(file)
    if rel is None or rel in _own:
        return _orig_open(file, mode, *args, **kwargs)
    try:
        with _orig_open(file, "rb") as f:
            data = f.read()
        mtime = _orig_stat(file).st_mtime
    except FileNotFoundError:
        _record_absent(rel)
        raise
    try:
        _record_file(rel, data, mtime)
    except Exception:  # noqa: BLE001 — recording never breaks a read
        pass
    return _as_file(data, mode, kwargs.get("encoding") or (args[1] if len(args) > 1 else None))


def _record_absent(rel: str) -> None:
    """A missing input file, once per disappearance (not once per poll)."""
    with _lock:
        if rel in _file_mt and _file_mt[rel] is None:
            return
        _file_mt[rel] = None
        _file_enc.pop(rel, None)
    _record({"ts": time.time(), "ch": "file", "f": rel, "absent": True})


def _live_stat(path, *a, **k):
    try:
        return _orig_stat(path, *a, **k)
    except FileNotFoundError:
        rel = _rel(path)
        if rel is not None and rel not in _own:
            _record_absent(rel)
        raise


def _as_file(data: bytes, mode: str, encoding: str | None):
    if "b" in mode:
        return io.BytesIO(data)
    return io.StringIO(data.decode(encoding or "utf-8"))


def _live_replace(src, dst, *a, **k):
    _note_write(dst)
    return _orig_replace(src, dst, *a, **k)


def _live_rename(src, dst, *a, **k):
    _note_write(dst)
    return _orig_rename(src, dst, *a, **k)


def _install_file_hooks(opener, stater, replacer, renamer) -> None:
    global _file_on
    builtins.open = opener
    io.open = opener
    os.stat = stater
    os.replace = replacer
    os.rename = renamer
    _file_on = True


def _uninstall_file_hooks() -> None:
    global _file_on
    builtins.open = _orig_open
    io.open = _orig_open
    os.stat = _orig_stat
    os.replace = _orig_replace
    os.rename = _orig_rename
    _file_on = False


# ── live mode ──────────────────────────────────────────────────────────────

def _live_request(self, method, path, data=None, base_url=None, api_version=None):
    t = time.time()
    base = str(base_url or getattr(self, "_base_url", "") or "")
    full = f"{base}/{api_version or getattr(self, '_api_version', '')}{path}"
    obj: dict[str, Any] = {"ts": t, "ch": "alpaca", "m": str(method).upper(), "p": full,
                           "q": _jsonable(data) if isinstance(data, dict) else data}
    try:
        resp = _orig_request(self, method, path, data=data, base_url=base_url,
                             api_version=api_version)
    except Exception as e:  # noqa: BLE001
        obj["err"] = f"{type(e).__name__}: {e}"[:300]
        obj["dt"] = round(time.time() - t, 3)
        _record(obj)
        raise
    obj["r"] = resp
    obj["dt"] = round(time.time() - t, 3)
    _record(obj)
    return resp


def install_live(*, files: bool = True) -> None:
    """Record every alpaca-py REST call and input-file read of this process."""
    global MODE, _orig_request
    from alpaca.common.rest import RESTClient
    with _lock:
        if _orig_request is None:
            _orig_request = RESTClient._request
        RESTClient._request = _live_request
        MODE = "live"
    if files:
        _install_file_hooks(_live_open, _live_stat, _live_replace, _live_rename)


# ── replay mode ────────────────────────────────────────────────────────────

class Recording:
    """The wire stream of one day, indexed for serving at a replay clock."""

    def __init__(self, path: Path, *, max_age: float = 600.0):
        self.max_age = max_age
        self.alpaca: dict[str, tuple[list[float], list[dict]]] = {}
        self.dash_ts: list[float] = []
        self._dash_recs: list[dict] = []
        self._file_recs: dict[str, list[dict]] = {}
        tmp: dict[str, list[tuple[float, dict]]] = {}
        if path.exists():
            with gzip.open(path, "rt") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("ch") == "alpaca":
                        k = alpaca_key(r.get("m", ""), _path_only(r.get("p", "")), r.get("q"))
                        tmp.setdefault(k, []).append((float(r["ts"]), r))
                    elif r.get("ch") == "dash":
                        self._dash_recs.append(r)
                    elif r.get("ch") == "file":
                        self._file_recs.setdefault(r["f"], []).append(r)
        for k, v in tmp.items():
            v.sort(key=lambda x: x[0])
            self.alpaca[k] = ([x[0] for x in v], [x[1] for x in v])
        self._dash_recs.sort(key=lambda r: float(r["ts"]))
        self.dash_ts = [float(r["ts"]) for r in self._dash_recs]
        self._dec = DashDecoder()
        self._dash_i = 0
        for v in self._file_recs.values():
            v.sort(key=lambda r: float(r["ts"]))
        self._file_ts = {k: [float(r["ts"]) for r in v] for k, v in self._file_recs.items()}
        self._file_dec: dict[str, DeltaDecoder] = {}
        self._file_i: dict[str, int] = {}
        self._file_state: dict[str, dict] = {}

    def alpaca_at(self, key: str, t: float) -> dict | None:
        ts, recs = self.alpaca.get(key, ([], []))
        i = bisect.bisect_right(ts, t) - 1
        if i < 0 or t - ts[i] > self.max_age:
            return None
        return recs[i]

    def dash_at(self, t: float) -> dict | None:
        """Payload of the last live fetch at or before t (forward-only)."""
        n = bisect.bisect_right(self.dash_ts, t)
        if n == 0:
            return None
        while self._dash_i < n:
            self._dec.apply(self._dash_recs[self._dash_i])
            self._dash_i += 1
        if t - self.dash_ts[n - 1] > self.max_age:
            return None
        return self._dec.payload(t)


_rec: Recording | None = None


def _file_at(rec: "Recording", rel: str, t: float) -> dict | None:
    """{'absent': True} | {'data': bytes, 'mt': float} as live last read it at
    or before t; None when the recording never saw this file by then."""
    ts = rec._file_ts.get(rel)
    if not ts:
        return None
    n = bisect.bisect_right(ts, t)
    if n == 0:
        return None
    recs = rec._file_recs[rel]
    dec = rec._file_dec.setdefault(rel, DeltaDecoder())
    st = rec._file_state.setdefault(rel, {})
    i = rec._file_i.get(rel, 0)
    while i < n:
        r = recs[i]
        if r.get("absent"):
            st.clear()
            st["absent"] = True
        else:
            st.pop("absent", None)
            st["mt"] = r.get("mt")
            if "raw" in r:
                st["raw"] = r["raw"]
            else:
                st.pop("raw", None)
                dec.apply(r)
        i += 1
    rec._file_i[rel] = i
    if st.get("absent"):
        return {"absent": True}
    if "raw" in st:
        return {"data": st["raw"].encode("utf-8"), "mt": st.get("mt")}
    return {"data": json.dumps(dec.document(t)).encode("utf-8"), "mt": st.get("mt")}


def _served_file(file) -> tuple[str, dict] | None:
    rel = _rel(file)
    if rel is None or rel in _own or _rec is None:
        return None
    hit = _file_at(_rec, rel, _clock())
    if hit is None:
        missed[f"file {rel}"] += 1
        miss_callers[_caller()] += 1
        return rel, {"absent": True}
    served[f"file {rel}"] += 1
    return rel, hit


def _replay_open(file, mode="r", *args, **kwargs):
    if not _is_read(mode):
        _note_write(file)
        return _orig_open(file, mode, *args, **kwargs)
    got = _served_file(file)
    if got is None:
        return _orig_open(file, mode, *args, **kwargs)
    rel, hit = got
    if hit.get("absent"):
        raise FileNotFoundError(2, "No such file (replay: absent or unrecorded)", rel)
    return _as_file(hit["data"], mode, kwargs.get("encoding") or (args[1] if len(args) > 1 else None))


def _replay_stat(path, *a, **k):
    got = _served_file(path) if _rel(path) is not None else None
    if got is None:
        return _orig_stat(path, *a, **k)
    rel, hit = got
    if hit.get("absent"):
        raise FileNotFoundError(2, "No such file (replay: absent or unrecorded)", rel)
    mt = float(hit.get("mt") or 0.0)
    size = len(hit["data"])
    return os.stat_result((_stat.S_IFREG | 0o644, 0, 0, 1, 0, 0, size,
                           int(mt), int(mt), int(mt), mt, mt, mt))


def _replay_replace(src, dst, *a, **k):
    _note_write(dst)
    return _orig_replace(src, dst, *a, **k)


def _replay_rename(src, dst, *a, **k):
    _note_write(dst)
    return _orig_rename(src, dst, *a, **k)


def _path_only(full: str) -> str:
    """'https://data.alpaca.markets/v2/stocks/bars' -> '/v2/stocks/bars'."""
    s = str(full or "")
    i = s.find("://")
    if i >= 0:
        j = s.find("/", i + 3)
        s = s[j:] if j >= 0 else "/"
    return s


def _miss(channel: str, what: str) -> ReplayMiss:
    missed[f"{channel} {what}"] += 1
    miss_callers[_caller()] += 1
    return ReplayMiss(f"replay has no recorded {channel} read for {what}")


def _replay_request(self, method, path, data=None, base_url=None, api_version=None):
    base = str(base_url or getattr(self, "_base_url", "") or "")
    full = f"{base}/{api_version or getattr(self, '_api_version', '')}{path}"
    key = alpaca_key(method, _path_only(full), _jsonable(data) if isinstance(data, dict) else data)
    hit = _rec.alpaca_at(key, _clock()) if _rec is not None else None
    if hit is None:
        raise _miss("alpaca", f"{str(method).upper()} {_path_only(full)}")
    served[f"alpaca {_path_only(full)}"] += 1
    if "err" in hit:
        raise ReplayMiss(f"recorded failure: {hit['err']}")
    return hit.get("r")


def serve_dash() -> dict:
    """Replay: the /api/state payload live had at the replay clock."""
    p = _rec.dash_at(_clock()) if _rec is not None else None
    if p is None:
        raise _miss("dash", "/api/state")
    served["dash /api/state"] += 1
    return _tracked(p)


def install_replay(path: Path, clock: Callable[[], float], *, max_age: float = 600.0,
                   files: bool = True, root: Path | None = None) -> Recording:
    """Serve every alpaca-py REST call, /api/state and input file from a
    recording. *root* is the replayed code's repo root (default: this file's)."""
    global ROOT
    global MODE, _orig_request, _rec, _clock
    from alpaca.common.rest import RESTClient
    with _lock:
        if _orig_request is None:
            _orig_request = RESTClient._request
        _rec = Recording(Path(path), max_age=max_age)
        _clock = clock
        RESTClient._request = _replay_request
        MODE = "replay"
    if root is not None:
        ROOT = Path(root)
    if files:
        _install_file_hooks(_replay_open, _replay_stat, _replay_replace, _replay_rename)
    return _rec


def uninstall() -> None:
    global MODE, _rec, _clock
    from alpaca.common.rest import RESTClient
    with _lock:
        if _orig_request is not None:
            RESTClient._request = _orig_request
        MODE, _rec, _clock = "off", None, time.time
    if _file_on:
        _uninstall_file_hooks()
    _own.clear()
    _file_enc.clear()
    _file_mt.clear()


def report() -> dict:
    return {"mode": MODE, "served": dict(served), "missed": dict(missed),
            "miss_callers": dict(miss_callers.most_common(20))}
