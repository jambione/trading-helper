"""Exclude earnings-flagged names from overnight top-20 12-1 momentum (liquid).
Uses Alpaca/Benzinga news for the selected ~20 symbols only (window: prior close 16:00 -> next open 09:35).
Resumable into /tmp/lh/earn_nights.json. Then prints gross/net with and without exclusions.
"""
import json, os, re, sys, time, collections
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import numpy as np, requests
from config import load_config
import lh_core as C
import lh_strat as S

ET = ZoneInfo("America/New_York")
OUT = "/tmp/lh"
EARN = re.compile(r"\b(earnings|EPS|Q[1-4]\b|quarter(ly)?|fiscal|results|revenue|sales|guidance|outlook|beats?|miss(es)?|tops|reports?)\b", re.I)
c = load_config() or {}
H = {"APCA-API-KEY-ID": c.get("api_key"), "APCA-API-SECRET-KEY": c.get("secret_key")}


def selections(P):
    T = P["c"].shape[0]
    momd = np.full_like(P["c"], np.nan)
    with np.errstate(all="ignore"):
        momd[252:] = P["cf"][231:-21] / P["cf"][:-252] - 1
    out = {}
    for t in range(252, T - 1):
        day = str(P["dates"][t].date())
        # Fetch cost is high; score the OOS window that decides the desk call (2022+).
        if day < "2022-01-01":
            continue
        x = np.where(P["liquid"][t] & np.isfinite(momd[t]), momd[t], np.nan)
        ok = np.where(np.isfinite(x))[0]
        if len(ok) < 30:
            continue
        j = ok[np.argsort(-x[ok])][:20]
        nxt = str(P["dates"][t + 1].date())
        out[day] = {"next": nxt, "syms": [str(P["syms"][i]) for i in j], "idx": [int(i) for i in j], "t": int(t)}
    return out


def fetch(sels):
    p = f"{OUT}/earn_nights.json"
    got = json.load(open(p)) if os.path.exists(p) else {}
    days = [d for d in sorted(sels) if d not in got]
    print("earn nights todo", len(days), "have", len(got), flush=True)
    t0 = time.time()
    for n, d in enumerate(days):
        x = sels[d]
        st = datetime.strptime(d, "%Y-%m-%d").replace(hour=16, tzinfo=ET)
        en = datetime.strptime(x["next"], "%Y-%m-%d").replace(hour=9, minute=35, tzinfo=ET)
        rec = {"earn_syms": [], "n_articles": 0}
        b = set(x["syms"]); tok = None
        while True:
            prm = {"symbols": ",".join(sorted(b)), "start": st.isoformat(), "end": en.isoformat(),
                   "limit": 50, "include_content": "false", "sort": "asc"}
            if tok:
                prm["page_token"] = tok
            for k in range(6):
                try:
                    r = requests.get("https://data.alpaca.markets/v1beta1/news", params=prm, headers=H, timeout=60)
                    if r.status_code == 429:
                        time.sleep(10); continue
                    r.raise_for_status(); j = r.json(); break
                except Exception as e:  # noqa: BLE001
                    print("err", type(e).__name__, str(e)[:80], flush=True); time.sleep(5 * (k + 1))
            else:
                raise SystemExit("earn fetch failed")
            for a in j.get("news", []):
                rec["n_articles"] += 1
                ss = a.get("symbols") or []; hl = a.get("headline") or ""
                if len(ss) <= 3 and EARN.search(hl):
                    for s in ss:
                        if s in b and s not in rec["earn_syms"]:
                            rec["earn_syms"].append(s)
            tok = j.get("next_page_token"); time.sleep(0.25)
            if not tok:
                break
        got[d] = rec
        if n % 25 == 0:
            json.dump(got, open(p, "w"))
            print(n, "/", len(days), d, "earn", len(rec["earn_syms"]), f"{time.time()-t0:.0f}s", flush=True)
    json.dump(got, open(p, "w"))
    print("done earn nights", len(got), flush=True)
    return got


def score(P, sels, got):
    ON = P["ON"]; d = P["dates"]; T = P["c"].shape[0]
    g_all = np.zeros(T); g_ex = np.zeros(T); n_drop = 0; n_night = 0
    for day, x in sels.items():
        t = x["t"]; j = np.array(x["idx"], int)
        on = ON[t + 1, j]; ok = np.isfinite(on)
        if ok.sum() == 0:
            continue
        g_all[t + 1] = on[ok].mean(); n_night += 1
        earn = set((got.get(day) or {}).get("earn_syms") or [])
        keep = [i for i, s in zip(j, x["syms"]) if s not in earn]
        if len(keep) < 5:
            n_drop += 1; continue
        on2 = ON[t + 1, keep]; ok2 = np.isfinite(on2)
        if ok2.sum():
            g_ex[t + 1] = on2[ok2].mean()
    print(f"nights {n_night}; nights dropped (<5 names after earn filter) {n_drop}")
    for lab, g in (("all top20", g_all), ("ex-earn names", g_ex)):
        n2 = (1 + g) * (1 - 0.0002) ** 2 - 1; n2[:253] = 0; g2 = g.copy(); g2[:253] = 0
        mg = C.metrics(g2, d, P["spy_r"]); mn = C.metrics(n2, d, P["spy_r"])
        print(f"### {lab} gross OOS {mg['OOS']['cagr']*100:+.1f}% mean {g2[253:][d[253:]>C.IS_END].mean()*1e4:.1f} bp; "
              f"2bp/side OOS {mn['OOS']['cagr']*100:+.1f}% a {mn['OOS']['alpha']*100:+.1f}% (t {mn['OOS']['alpha_t']:.1f})")


if __name__ == "__main__":
    P = S.returns(C.load())
    sels = selections(P)
    json.dump({k: {"next": v["next"], "syms": v["syms"], "t": v["t"]} for k, v in sels.items()},
              open(f"{OUT}/on_sels.json", "w"))
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    if mode in ("fetch", "all"):
        got = fetch(sels)
    else:
        got = json.load(open(f"{OUT}/earn_nights.json"))
    if mode in ("score", "all"):
        score(P, sels, got)
