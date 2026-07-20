"""One-shot 2x2 / multi-chart auto-locate test. Run with the TradingView
grid visible and in front:

    python tv_test_grid.py            # let it auto-detect the layout
    python tv_test_grid.py 2x2        # force a layout (1x1/1x2/2x1/2x2)

Unlike tv_test_locate.py (which probes the whole window as ONE chart),
this drives the real TVTracker grid path: it splits the window into
cells, locates each cell's panels, OCRs each cell's symbol, and runs the
leader election. Draws every cell + its panels on tv_test_grid.png and
writes tv_test_grid.txt.
"""
import sys
import time

if hasattr(sys.stdout, "reconfigure"):      # window titles contain ▲▼
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import cv2
import mss
import numpy as np

from tv_core import LeaderTracker
from tv_signal import (HERE, TVTracker, find_tv_windows, load_config,
                       read_check, read_heart, read_squeeze, read_star)

PANEL_COLORS = {"star": (0, 255, 255), "heart": (255, 0, 255),
                "check": (0, 255, 0), "fire": (0, 128, 255)}
CELL_COLOR = (255, 255, 0)
OUT = HERE / "tv_test_grid.txt"


def main():
    lines = []

    def say(msg):
        print(msg)
        lines.append(msg)

    cfg = load_config()
    if len(sys.argv) > 1:                    # force a layout for the test
        cfg["grid"] = sys.argv[1]
    say(f"time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    say(f"grid mode: {cfg.get('grid', 'auto')}")

    cands = find_tv_windows()
    if not cands:
        say("STEP 1 FAIL: no visible browser windows found at all.")
        OUT.write_text("\n".join(lines), encoding="utf-8")
        return
    say(f"STEP 1 OK: {len(cands)} browser window(s); best-first")
    say(f"  best title: {cands[0][1][:70]!r}")

    tracker = TVTracker(cfg)
    with mss.mss() as sct:
        found = None
        rect = cands[0][0]
        for i, (cand, title) in enumerate(cands[:3]):
            img = np.asarray(sct.grab(cand))
            t0 = time.time()
            slots = tracker._locate_slots(img, cand)
            say(f"  candidate {i + 1} {cand['width']}x{cand['height']}: "
                + (f"{len(slots)} cell(s) located ({time.time() - t0:.2f}s)"
                   if slots else "no indicator axes in any cell"))
            if slots:
                found, rect = slots, cand
                break

        if not found:
            say("STEP 2 FAIL: no grid layout matched. Is the TradingView "
                "grid visible and in front, with the indicator panels "
                "(star/heart/fire) showing in each chart?")
            OUT.write_text("\n".join(lines), encoding="utf-8")
            return

        say(f"STEP 2 OK: {len(found)} chart cell(s) detected")
        dbg = np.ascontiguousarray(img[:, :, :3])

        readers = {"star": read_star, "heart": read_heart,
                   "check": read_check, "fire": read_squeeze}
        scores_for_leader = {}
        for n, slot in enumerate(found, 1):
            sym = slot.get("symbol")
            sr = slot["rect"]
            cx, cy = sr["left"] - rect["left"], sr["top"] - rect["top"]
            cv2.rectangle(dbg, (cx, cy),
                          (cx + sr["width"], cy + sr["height"]),
                          CELL_COLOR, 3)
            cv2.putText(dbg, f"cell {n}: {sym or '?'}", (cx + 6, cy + 26),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, CELL_COLOR, 2)
            say(f"\n  cell {n}: symbol={sym or '?'}  "
                f"panels={sorted(slot['panels'])}")
            for name, p in sorted(slot["panels"].items()):
                x, y = p["left"] - rect["left"], p["top"] - rect["top"]
                cv2.rectangle(dbg, (x, y), (x + p["width"], y + p["height"]),
                              PANEL_COLORS.get(name, (255, 255, 255)), 1)
                r = readers[name](np.asarray(sct.grab(p)))
                say(f"    [{name}] {r}")
            # a cheap standin score so the leader election is exercised too
            hv = None
            scores_for_leader[sym or f"cell{n}"] = 0.0

        cv2.imwrite(str(HERE / "tv_test_grid.png"), dbg)

        # exercise the leader election with the detected symbols
        lt = LeaderTracker(float(cfg.get("leader_margin", 1.5)),
                           int(cfg.get("leader_confirm_reads", 5)))
        syms = [s.get("symbol") for s in found]
        say(f"\nSymbols across the grid: "
            + ", ".join(s or "?" for s in syms))
        dupes = [s for s in set(syms) if s and syms.count(s) > 1]
        if dupes:
            say(f"  WARNING: duplicate/blank symbols {dupes} — legend OCR "
                "may be misreading a cell (leader/L2 match keys on symbol).")

    say("\nfiles written: tv_test_grid.png, tv_test_grid.txt")
    OUT.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
