#!/usr/bin/env python3
"""auto_picker.py - the hands-off pick ledger (operator 11/11: "this project is for me to be hands off").

Once a week, before the open, each of the desk's two AI agents (AGY/Gemini and Grok) is shown the week's headlines for
liquid stocks and asked for its best few names to hold for months. The picks are recorded in that agent's own ledger
with tools/pick_ledger.py (stamped, hash-chained, committed in the ledger's own git repo), and scored later against SPY
and random names by the same rules. Nobody has to do anything. NOTHING HERE PLACES AN ORDER.

    .venv/bin/python tools/auto_picker.py run        # record this week's picks for both agents, then write the report
    .venv/bin/python tools/auto_picker.py report     # score both ledgers -> ~/pick_ledgers/REPORT.txt
    .venv/bin/python tools/auto_picker.py replay     # twelve PAST Mondays from the news archive: a quick screen (from the mini's Terminal)

Must run inside the mini's logged-in session (a LaunchAgent), because the agents' logins live in the Keychain and a
process started over SSH cannot read them. Install: scripts/install_auto_picker.sh. Rules: docs/studies/auto_picker_prereg.json.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(os.environ.get("REPO") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(ROOT), str(ROOT / "tools")]
import pick_ledger as P

ET = ZoneInfo("America/New_York")
HOME = Path(os.environ.get("PICK_LEDGERS") or Path.home() / "pick_ledgers")
AGENTS = ("agy", "grok")
PER_WEEK, ASK_FOR, MAX_HEADLINES = 3, 6, 220


def ledger_path(agent: str) -> Path:
    return HOME / agent / "pick_ledger.jsonl"


def ensure_repo(agent: str) -> Path:
    d = HOME / agent
    d.mkdir(parents=True, exist_ok=True)
    if not (d / ".git").exists():
        subprocess.run(["git", "-C", str(d), "init", "-q"], check=True)
    return d


def headlines(universe: set[str], days: int = 7, limit: int = MAX_HEADLINES, end: datetime | None = None) -> list[str]:
    """Headlines from the `days` before `end` (default now) that name a liquid stock, newest first: 'SYM[,SYM]: headline'."""
    from alpaca.data.historical.news import NewsClient
    from alpaca.data.requests import NewsRequest
    with open(ROOT / "secrets_data.json") as fh:
        s = json.load(fh)
    cl = NewsClient(s["alpaca_data_api_key"], s["alpaca_data_secret_key"])
    out, seen, token = [], set(), None
    stop = (end or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start = stop - timedelta(days=days)
    for _ in range(12):
        r = cl.get_news(NewsRequest(start=start, end=stop, limit=50, page_token=token, sort="desc"))
        rows = r.data.get("news", [])
        for x in rows:
            syms = [y for y in (x.symbols or []) if y in universe][:3]
            h = re.sub(r"\s+", " ", x.headline or "").strip()
            if syms and h and h not in seen:
                seen.add(h)
                out.append(f"{','.join(syms)}: {h[:140]}")
        token = getattr(r, "next_page_token", None)
        if not token or len(out) >= limit:
            break
    return out[:limit]


def build_prompt(today: str, heads: list[str], universe: list[str], held: list[str]) -> str:
    """The wording is fixed; only the date, the week's headlines and the recent-picks line change.
    11/11: the first version pasted all 1,931 allowed symbols. AGY (an agent) answered by trying to run a Python command to
    check its picks against that list; headless mode refused the command and the session ended with no reply. The list is
    gone from the prompt: every symbol in the headlines is already on it, and the code drops anything that is not."""
    del universe                                  # kept in the signature; the allowed list is enforced in parse(), not pasted
    return (
        f"Today is {today}. You are choosing stocks for a paper record that tests whether your picks beat the S&P 500.\n"
        "No money is traded. Each pick is bought on paper at the next market open and judged against SPY after about three months.\n\n"
        "Answer directly in your reply. Do NOT run commands, write files, browse, or use any tool: everything you need is in this message.\n\n"
        f"Choose your {ASK_FOR} best ideas, best first, for a holding period of 3 to 12 months. Rules:\n"
        "- US common stocks only. Every symbol that appears before a colon in the HEADLINES below is allowed.\n"
        "  You may also name another large, liquid US common stock priced above $10; anything not allowed is simply dropped.\n"
        "- Base each idea on a business reason (earnings, a product, demand, a contract, a turnaround), not on a chart.\n"
        "- Do not invent facts or numbers. If you rely on a headline below, say which one in a few words.\n"
        + (f"- Do not repeat these recent picks: {', '.join(held)}.\n" if held else "")
        + "\nReply with JSON ONLY, exactly this shape:\n"
        '{"picks": [{"sym": "ABC", "thesis": "one or two sentences on why it should beat the market", "months": 6, '
        '"sell_if": "what would prove the idea wrong"}]}\n\n'
        f"HEADLINES from the past week ({len(heads)}):\n" + "\n".join(heads) + "\n"
    )


def parse(text: str, universe: set[str]) -> list[dict]:
    """The model's picks in its own order. Anything malformed, off-list or thin is dropped; a bad reply is an empty list."""
    m = re.search(r"\{.*\}", text or "", flags=re.DOTALL)
    if not m:
        return []
    try:
        rows = json.loads(m.group(0)).get("picks", [])
    except (ValueError, AttributeError):
        return []
    out, seen = [], set()
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        sym = str(r.get("sym", "")).strip().upper()
        thesis, sell_if = str(r.get("thesis", "")).strip(), str(r.get("sell_if", "")).strip()
        try:
            months = float(r.get("months", 6))
        except (TypeError, ValueError):
            months = 6.0
        if sym in universe and sym not in seen and len(thesis) >= 15 and len(sell_if) >= 8:
            seen.add(sym)
            out.append({"sym": sym, "thesis": thesis[:400], "months": min(12.0, max(3.0, months)), "sell_if": sell_if[:200]})
    return out


def ask_agent(agent: str, prompt: str) -> tuple[str, str]:
    """(reply text, model name). Uses the desk's own CLI wrappers."""
    from ai_suggest import call_agy_cli, call_grok_cli
    from config import load_config_ro
    cfg = load_config_ro() or {}
    if agent == "agy":
        model = str(cfg.get("weekly_agy_model") or cfg.get("agy_model") or "gemini-3.8-flash-high")
        return call_agy_cli(prompt, model=model, live_search=False, phase="auto_picker"), model
    model = str(cfg.get("grok_model") or "grok")
    return call_grok_cli(prompt, model=model, live_search=False, phase="auto_picker"), model


def record(agent: str, picks: list[dict], *, model: str, prompt_sha: str, n_heads: int, now: datetime | None = None,
           universe: list[str] | None = None) -> list[dict]:
    """Record up to PER_WEEK picks in the agent's ledger; a repeat inside 92 days is skipped and the next idea is used."""
    path = ledger_path(agent)
    done = []
    for p in picks:
        if len(done) >= PER_WEEK:
            break
        try:
            r = P.add_pick(p["sym"], p["thesis"], p["months"], p["sell_if"], now=now, path=path, universe=universe,
                           extra={"source": agent, "model": model, "prompt_sha": prompt_sha, "headlines": n_heads})
        except ValueError:
            continue
        if not P.git_commit(path, r["symbol"]):
            raise RuntimeError(f"{agent}: could not commit {r['symbol']} in {path.parent}")
        done.append(r)
    return done


def this_week_done(agent: str, now: datetime) -> bool:
    wk = now.isocalendar()[:2]
    return any(datetime.fromisoformat(p["recorded_at"]).isocalendar()[:2] == wk for p in P.read_ledger(ledger_path(agent)))


def report() -> str:
    parts = [f"Automatic pick ledgers, written {datetime.now(ET):%Y-%m-%d %H:%M} ET. Paper record only; no orders are placed.\n"]
    for agent in AGENTS:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                P.cmd_score(ledger_path(agent))
            except Exception as e:  # noqa: BLE001
                print(f"could not score: {type(e).__name__} {e!s:.200}")
        parts.append(f"===== {agent.upper()} =====\n{buf.getvalue()}")
    text = "\n".join(parts)
    HOME.mkdir(parents=True, exist_ok=True)
    (HOME / "REPORT.txt").write_text(text)
    return text


def run(now: datetime | None = None) -> int:
    now = now or datetime.now(ET)
    uni = P.frozen_universe()
    if len(uni) < 500:
        print("the frozen universe list is missing; nothing recorded")
        return 1
    log = []
    try:
        heads = headlines(set(uni))
    except Exception as e:  # noqa: BLE001
        heads = []
        log.append(f"headlines failed: {type(e).__name__} {e!s:.120}")
    if len(heads) < 30:
        print(f"only {len(heads)} headlines; a pick made without news would test the model's old memory, not its judgment. Nothing recorded. {log}")
        return 1
    for agent in AGENTS:
        ensure_repo(agent)
        if this_week_done(agent, now):
            log.append(f"{agent}: already recorded this week")
            continue
        recent = [p["symbol"] for p in P.read_ledger(ledger_path(agent))
                  if (now - datetime.fromisoformat(p["recorded_at"])).days < P.DEDUPE_DAYS]
        prompt = build_prompt(now.strftime("%Y-%m-%d"), heads, uni, recent)
        sha = hashlib.sha1(build_prompt("DATE", [], uni, []).encode()).hexdigest()[:10]       # the fixed wording, without the week's news
        text = model = err = None
        for _attempt in range(2):                                         # one retry with the SAME prompt (an empty reply is often transient)
            try:
                text, model = ask_agent(agent, prompt)
                if parse(text, set(uni)):
                    break
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__} {e!s:.120}"
        if not text:
            log.append(f"{agent}: no reply after 2 tries ({err})")
            continue
        raw = HOME / agent / "raw"
        raw.mkdir(exist_ok=True)
        (raw / f"{now:%Y-%m-%d}.txt").write_text(str(text or ""))
        picks = parse(text, set(uni))
        done = record(agent, picks, model=model, prompt_sha=sha, n_heads=len(heads), now=now)
        log.append(f"{agent} ({model}): {len(picks)} valid ideas, recorded {', '.join(r['symbol'] for r in done) or 'none'}")
    print("\n".join(log))
    print(report())
    return 0


# ---------- replay on past weeks (docs/studies/auto_picker_replay_prereg.json) ----------
REPLAY_LAST, REPLAY_WEEKS = "2026-10-12", 12          # twelve Mondays, 2026-07-27 .. 2026-10-12: their 21-session windows have closed
PROBE = ("Answer from memory only, with no tools. What was the S&P 500's closing level on 2026-09-30, and what were three notable US "
         "stock-market events in September 2026? If you do not know, reply with the single word UNKNOWN.")


def replay_mondays(last: str = REPLAY_LAST, weeks: int = REPLAY_WEEKS) -> list[datetime]:
    y, m, d = map(int, last.split("-"))
    end = datetime(y, m, d, 8, 30, tzinfo=ET)
    return [end - timedelta(days=7 * k) for k in range(weeks - 1, -1, -1)]


def replay_line(prev: str, agent: str, p: dict, when: datetime, pool: list[str]) -> dict:
    """A replay pick in the ledger's record shape (so pick_ledger scores it), outside the live hash-chained ledgers."""
    import random
    stamp = when.isoformat(timespec="seconds")
    rid = P.record_id(prev, p["sym"], stamp, p["thesis"])
    return {"id": rid, "prev": prev, "symbol": p["sym"], "recorded_at": stamp, "thesis": p["thesis"], "hold_months": p["months"],
            "sell_if": p["sell_if"], "retro": False, "source": f"replay_{agent}",
            "controls": random.Random(rid).sample(sorted(x for x in pool if x != p["sym"]), P.N_CONTROL)}


def take_three(ideas: list[dict], taken: dict[str, datetime], when: datetime) -> list[dict]:
    out = []
    for p in ideas:
        if len(out) >= PER_WEEK:
            break
        if p["sym"] in taken and (when - taken[p["sym"]]).days < P.DEDUPE_DAYS:
            continue
        taken[p["sym"]] = when
        out.append(p)
    return out


def replay_collect(ask=None, heads_for=None) -> dict[str, list[dict]]:
    """Ask each agent for picks on each past Monday from that week's headlines; also a no-skill control that takes three
    names at random from the same headlines. ask(agent, prompt) -> (text, model) and heads_for(monday) replace I/O in tests."""
    import random
    uni = P.frozen_universe()
    ask = ask or ask_agent
    heads_for = heads_for or (lambda mon: headlines(set(uni), end=mon))
    out_dir = HOME / "replay"
    (out_dir / "raw").mkdir(parents=True, exist_ok=True)
    lines: dict[str, list[dict]] = {a: [] for a in (*AGENTS, "random")}
    taken: dict[str, dict] = {a: {} for a in lines}
    for mon in replay_mondays():
        heads = heads_for(mon)
        if len(heads) < 30:
            print(f"{mon:%Y-%m-%d}: only {len(heads)} headlines, week skipped", flush=True)
            continue
        named = sorted({x for h in heads for x in h.split(":")[0].split(",")})
        rng = random.Random(f"replay-{mon:%Y-%m-%d}")
        ideas = {"random": [{"sym": x, "thesis": "no-skill control: a random name from the week's headlines", "months": 6.0, "sell_if": "not applicable"}
                            for x in rng.sample(named, min(len(named), 12))]}
        for agent in AGENTS:
            raw = out_dir / "raw" / f"{agent}_{mon:%Y-%m-%d}.txt"
            if raw.exists():
                text = raw.read_text()                                    # resumable: a week already answered is not asked again
            else:
                text = ""
                for _try in range(2):
                    try:
                        text, _model = ask(agent, build_prompt(mon.strftime("%Y-%m-%d"), heads, uni, sorted(taken[agent])))
                    except Exception as e:  # noqa: BLE001
                        text = ""
                        print(f"{mon:%Y-%m-%d} {agent}: {type(e).__name__} {e!s:.100}", flush=True)
                    if parse(text, set(uni)):
                        break
                raw.write_text(str(text or ""))
            ideas[agent] = parse(text, set(uni))
        for a in lines:
            for p in take_three(ideas.get(a, []), taken[a], mon):
                lines[a].append(replay_line(lines[a][-1]["id"] if lines[a] else "", a, p, mon, uni))
        print(f"{mon:%Y-%m-%d}: {len(heads)} headlines; " + ", ".join(f"{a} {len(ideas.get(a, []))} ideas" for a in AGENTS), flush=True)
    for a, rows in lines.items():
        (out_dir / f"{a}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return lines


def replay_score(lines: dict[str, list[dict]] | None = None) -> str:
    """Score the replay picks at 21 sessions with the ledger's rules (no commit check: these are not live records)."""
    out_dir = HOME / "replay"
    lines = lines or {a: P.read_ledger(out_dir / f"{a}.jsonl") for a in (*AGENTS, "random")}
    picks = [p for rows in lines.values() for p in rows]
    if not picks:
        return "no replay picks"
    start = min(datetime.fromisoformat(p["recorded_at"]) for p in picks) - timedelta(days=420)
    need = sorted({p["symbol"] for p in picks} | {"SPY"} | {c for p in picks for c in p["controls"]})
    bars = P.fetch(need, start)
    days = sorted(bars["SPY"])
    rows = [f"REPLAY on past headlines, {replay_mondays()[0]:%Y-%m-%d} to {REPLAY_LAST}, scored {P.EARLY} sessions after entry. "
            "A SCREEN, not proof: an agent may have been trained on this period.", ""]
    res = {}
    for a, lst in lines.items():
        scored = [P.score_pick(p, days, bars, commit_ts=0.0) for p in lst]
        ok = [x for x in scored if x["status"] == "scored" and x["h"].get(P.EARLY, {}).get("closed")]
        if len(ok) < 12:
            rows.append(f"{a:<7} only {len(ok)} scoreable picks")
            continue
        v = P.verdict(ok, h=P.EARLY, need=len(ok))
        res[a] = v
        if "mean_vs_spy" not in v:
            rows.append(f"{a:<7} {len(ok)} picks: {v['verdict']} {v.get('why', '')}")
            continue
        rows.append(f"{a:<7} {len(ok):3d} picks  vs SPY mean {P.pct(v['mean_vs_spy'])} median {P.pct(v['median_vs_spy'])}  t {v['t']:+.2f} ({v['months']} months)  "
                    f"ahead {100 * v['ahead']:.0f}%  vs random names {P.pct(v['mean_vs_random'])}  rank {100 * v['mean_rank']:.0f}th pct  beta-adj {P.pct(v['mean_beta_excess'])}")
    rnd = res.get("random", {}).get("mean_vs_spy")
    rows.append("")
    for a in AGENTS:
        v = res.get(a)
        if not v or "mean_vs_spy" not in v:
            continue
        sign = v["verdict"] == "PASS" and (rnd is None or v["mean_vs_spy"] > rnd)
        rows.append(f"{a.upper()}: {'SIGN OF SKILL (worth the live wait)' if sign else 'NO SIGN OF SKILL'}"
                    + (f"; random names from the same headlines made {P.pct(rnd)} vs SPY" if rnd is not None else ""))
    text = "\n".join(rows)
    (out_dir / "REPORT.txt").write_text(text + "\n")
    return text


def replay() -> int:
    out_dir = HOME / "replay"
    out_dir.mkdir(parents=True, exist_ok=True)
    for agent in AGENTS:                                                  # does the agent remember the period? shown to the operator, not graded
        f = out_dir / f"probe_{agent}.txt"
        if not f.exists():
            try:
                f.write_text(str(ask_agent(agent, PROBE)[0] or ""))
            except Exception as e:  # noqa: BLE001
                f.write_text(f"(probe failed: {type(e).__name__} {e!s:.200})")
        print(f"MEMORY PROBE {agent}: {f.read_text().strip()[:400]}\n", flush=True)
    print(replay_score(replay_collect()))
    return 0


if __name__ == "__main__":
    cmd = (sys.argv[1:] or ["run"])[0]
    sys.exit(run() if cmd == "run" else replay() if cmd == "replay" else (print(replay_score()) or 0) if cmd == "replay-score" else (print(report()) or 0))
