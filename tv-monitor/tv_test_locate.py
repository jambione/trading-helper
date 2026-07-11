"""One-shot TV auto-locate test. Run with the TradingView chart visible:

    python tv_test_locate.py

Draws a colored box per located panel on tv_test_window.png and writes
tv_test_result.txt.
"""
import sys
import time

if hasattr(sys.stdout, "reconfigure"):      # window titles contain ▲▼
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import cv2
import mss
import numpy as np

from tv_signal import (HERE, find_tv_windows, load_config, locate_tv_panels,
                       read_check, read_heart, read_squeeze, read_star)

COLORS = {"star": (0, 255, 255), "heart": (255, 0, 255),
          "check": (0, 255, 0), "fire": (0, 128, 255)}
OUT = HERE / "tv_test_result.txt"


def main():
    lines = []

    def say(msg):
        print(msg)
        lines.append(msg)

    load_config()
    say(f"time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    cands = find_tv_windows()
    if not cands:
        say("STEP 1 FAIL: no visible browser windows found at all.")
        OUT.write_text("\n".join(lines), encoding="utf-8")
        return
    say(f"STEP 1 OK: {len(cands)} browser window(s); trying best-first")
    say(f"  best title: {cands[0][1][:70]!r}")

    panels = None
    rect = cands[0][0]
    with mss.MSS() as sct:
        for i, (cand, title) in enumerate(cands[:3]):
            img = np.asarray(sct.grab(cand))
            t0 = time.time()
            panels = locate_tv_panels(img, cand)
            say(f"  candidate {i + 1} {cand}: "
                + (f"panels found ({time.time() - t0:.2f}s)" if panels
                   else "no indicator axes here"))
            if panels:
                rect = cand
                break
        say(f"STEP 2 {'OK' if panels else 'FAIL'}: panels = "
            + (", ".join(sorted(panels)) if panels else
               "none found - is the chart tab visible with the "
               "indicator panels on screen?"))
        dbg = np.ascontiguousarray(img[:, :, :3])
        if panels:
            for name, p in panels.items():
                x, y = p["left"] - rect["left"], p["top"] - rect["top"]
                cv2.rectangle(dbg, (x, y),
                              (x + p["width"], y + p["height"]),
                              COLORS.get(name, (255, 255, 255)), 2)
                cv2.putText(dbg, name, (x + 4, y + 16),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                            COLORS.get(name, (255, 255, 255)), 1)
        cv2.imwrite(str(HERE / "tv_test_window.png"), dbg)

        if panels:
            from tv_core import rightmost_data_x
            readers = {"star": read_star, "heart": read_heart,
                       "check": read_check, "fire": read_squeeze}
            colorsets = {"star": ("white", "red", "green"),
                         "heart": ("white", "blue"),
                         "check": ("yellow", "green", "red"),
                         "fire": ("green", "red")}
            for name, p in sorted(panels.items()):
                crop = np.asarray(sct.grab(p))
                cv2.imwrite(str(HERE / f"tv_panel_{name}.png"),
                            np.ascontiguousarray(crop[:, :, :3]))
                xe = rightmost_data_x(crop, colorsets[name])
                r = readers[name](crop)
                say(f"STEP 3 [{name}]: x_end={xe}/{crop.shape[1]}  {r}")

    say("files written: tv_test_window.png, tv_test_result.txt")
    OUT.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
