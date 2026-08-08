#!/usr/bin/env python3
"""Server-side Stocktwits trending poll.

Publishes trending_stocks.json, which dashboard.py merges into /api/state so
the momentum monitor can render the panel without polling Stocktwits itself.

Stocktwits' trending payload carries no usable quotes — price, %chg and volume
are all backfilled from Alpaca, so rows land here already enriched.

LOOK badges and the max_price panel filter are deliberately NOT applied here:
those thresholds are desk display settings that live in the monitor's
momentum_config.json and are applied by display_rows() at render time. This
file publishes the full ranked list.

    python3 trending_screener.py
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


import desk_core  # noqa: E402

# Quote enrichment needs Alpaca keys, which live only in signal_engine.env.
_loaded_env_keys = desk_core.load_env_file(ROOT / "signal_engine.env")
if _loaded_env_keys:
    print(f"[ENV] Loaded {len(_loaded_env_keys)} setting(s) from signal_engine.env",
          flush=True)

from config import load_config  # noqa: E402
from stocktwits_trending import (  # noqa: E402
    StocktwitsTrending, apply_look_highlights,
)

TRENDING_FILE = ROOT / "trending_stocks.json"

LOOP_SLEEP = 5.0


_write_json = desk_core.write_json_atomic


def main() -> None:
    cfg = load_config()

    st = StocktwitsTrending(
        poll_interval=float(cfg.get("stocktwits_poll", 60.0)),
        stocks_only=bool(cfg.get("stocktwits_stocks_only", True)),
        max_price=None,          # panel filter — applied by the monitor
        quote_interval=float(cfg.get("stocktwits_quote_poll", 15.0)),
        volume_interval=float(cfg.get("stocktwits_volume_poll", 60.0)),
        avg_days=int(cfg.get("stocktwits_avg_days", 10)),
        rvol_time_adjusted=bool(cfg.get("stocktwits_rvol_time_adjusted", True)),
        look_max=int(cfg.get("ai_watch_look_max", 20)),
        # Same RVOL floor AI Watch admission uses — one knob, not a duplicate.
        look_min_rvol=float(cfg.get("ai_watch_min_rvol", 1.5) or 0.0) or None,
    )

    print(f"[trending] polling Stocktwits every {st.poll_interval:.0f}s "
          f"(quotes {st.quote_interval:.0f}s, volume {st.volume_interval:.0f}s)",
          flush=True)

    while True:
        t0 = time.time()

        if not st.refresh(t0):
            st.refresh_quotes(t0)
            st.refresh_volume(t0)

        # Tag LOOK (EXT near 52w high / WASH near 52w low) before writing.
        # This file is what AI Watch admission reads, and it previously held
        # the raw rows: apply_look_highlights only ran inside snapshot(), which
        # this loop never calls, so look_reason never reached the gate and the
        # EXT requirement could not pass for any name, ever.
        #
        # Three consumers were disagreeing about which names are LOOK — the
        # terminal (via snapshot), the browser (its own JS reimplementation in
        # feeds.js), and admission (which saw nothing at all). Now the file
        # carries it, so the server-side path agrees with what is on screen.
        #
        # Shallow copies: st.rows stays clean for snapshot(), which applies its
        # own highlighting with the same parameters.
        rows_out = [dict(r) for r in st.rows]
        try:
            rows_out = apply_look_highlights(
                rows_out,
                min_abs_chg=st.look_min_abs_chg,
                max_looks=st.look_max,
                near_high=st.look_near_high,
                near_low=st.look_near_low,
                min_rvol=st.look_min_rvol,
            )
        except Exception as e:  # never let tagging stop the feed
            print(f"[trending] look tagging failed: {e}", flush=True)

        # Age the carried-forward rvol so a consumer can tell a live reading
        # from one held over a rate-limit backoff (see refresh()).
        for r in rows_out:
            ts = r.get("rvol_ts")
            r["rvol_age_sec"] = (t0 - float(ts)) if ts else None

        # Volume health, alongside the quote health already published. Without
        # it, rvol going None across every row — which silently drops the AI
        # Watch shortlist to "Stocktwits score alone" — is indistinguishable
        # from a genuinely flat session, and nothing anywhere says a word.
        # That is what happened on 2026-08-07: 26 rows, rvol null on all of
        # them from the open, no error published and none logged.
        n_rvol = sum(1 for r in rows_out if r.get("rvol") is not None)
        _write_json(TRENDING_FILE, {
            "updated": t0,
            "last_ok": st.last_ok,
            "error": st.error,
            "quotes_error": st.quotes_error,
            "last_quote_ok": st.last_quote_ok,
            "last_volume_ok": st.last_volume_ok,
            "volume_error": st.volume_error,
            "rvol_coverage": f"{n_rvol}/{len(rows_out)}",
            "rows": rows_out,
        })
        if rows_out and not n_rvol:
            print("[trending] WARNING rvol is None on all "
                  f"{len(rows_out)} rows — admission is running on "
                  "Stocktwits score alone", flush=True)

        time.sleep(LOOP_SLEEP)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[trending] stopped.", flush=True)
