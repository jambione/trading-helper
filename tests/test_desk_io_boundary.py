"""No network read in the desk process may bypass desk_io.

desk_io records alpaca-py at RESTClient._request, /api/state at
ai_entry_watch.dashboard_state and input files at open()/stat(). Anything
else that reaches the network from inside ai_trader is a read a replay cannot
serve, so it would silently diverge again. This walks every repo module
reachable from ai_trader (imports inside functions included) and fails on a
network call site that is not in ALLOWED, each of which says why it is safe.
"""
import ast
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

NET = re.compile(
    r"\burlopen\(|\brequests\.(get|post|put|delete|request|Session)\b|\bhttpx\.|"
    r"\baiohttp\.|\bsocket\.(socket|create_connection)|\bwebsocket|StockDataStream|"
    r"finnhub\.Client|\.urlretrieve\(")

ALLOWED = {
    # The dashboard transport. Its one read, GET /api/state, is recorded and
    # served by dashboard_state; its POST (/api/tickers/add) is an output.
    "ai_entry_watch._dash_urlopen": "wrapped by dashboard_state (dash channel)",
    "desk_auth._post_login": "dashboard login for _dash_urlopen",
    "desk_auth.urlopen": "dashboard transport for _dash_urlopen",
    "desk_auth._open": "dashboard transport for _dash_urlopen",
    # Research LLM calls. Their results reach the book through the research
    # board files, which the file channel records as the desk reads them.
    "ai_suggest._post_responses": "research output lands in *_suggestions.json (file channel)",
    "ai_suggest._post_responses_stream": "research output lands in *_suggestions.json (file channel)",
    # Imported by the desk for helpers only; these functions run in other
    # processes (dashboard stream, engine bars, screeners), whose output the
    # desk reads through /api/state or files.
    "finnhub_stream._finnhub_stream": "runs in dashboard.py",
    "finnhub_stream.start_finnhub_stream": "runs in dashboard.py",
    "finnhub_stream.fetch_realtime_quote": "runs in dashboard.py",
    "stream_bars.on_trade": "runs in signal_engine.py",
    "float_feed._fetch_one": "runs in movers_screener.py; desk reads float_cache.json (file channel)",
    "stocktwits_trending.fetch_trending": "runs in trending_screener.py",
}


def _modfile(name):
    for base in (ROOT, os.path.join(ROOT, "tools")):
        f = os.path.join(base, name.replace(".", "/") + ".py")
        if os.path.exists(f):
            return f
    return None


def _closure(entry="ai_trader"):
    seen, stack = {}, [entry]
    while stack:
        m = stack.pop()
        if m in seen:
            continue
        f = _modfile(m)
        if not f:
            continue
        seen[m] = f
        for node in ast.walk(ast.parse(open(f).read())):
            if isinstance(node, ast.Import):
                stack += [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                stack.append(node.module.split(".")[0])
    return seen


def _net_sites(mods):
    out = {}
    for m, f in mods.items():
        src = open(f).read()
        lines = src.splitlines()
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            inner = set()
            for c in ast.walk(node):
                if c is not node and isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    inner.update(range(c.lineno, c.end_lineno + 1))
            body = "\n".join(lines[i - 1] for i in range(node.lineno, node.end_lineno + 1)
                             if i not in inner and not lines[i - 1].strip().startswith("#"))
            if NET.search(body):
                out[f"{m}.{node.name}"] = f
    return out


def test_every_network_site_in_the_desk_is_accounted_for():
    mods = _closure()
    assert "ai_entry_watch" in mods and "desk_io" in mods
    sites = _net_sites(mods)
    unknown = sorted(set(sites) - set(ALLOWED))
    assert not unknown, (
        "network call(s) in the desk process that desk_io does not record — route "
        "them through desk_io or add them to ALLOWED with the reason they are safe: "
        f"{unknown}")


def test_allowlist_has_no_dead_entries():
    sites = _net_sites(_closure())
    dead = sorted(set(ALLOWED) - set(sites))
    assert not dead, f"ALLOWED names sites that no longer exist: {dead}"
