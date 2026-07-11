"""One-shot auto-locate test. Run with Webull open and the L2 panel visible:

    python test_locate.py

Prints each step, saves annotated images, and writes test_locate_result.txt.
No monitoring loop, no alerts - just the locate chain, once.
"""
import time

import cv2
import mss
import numpy as np

# importing l2_signal also sets DPI awareness + tesseract path via config
from l2_signal import (HERE, detect_symbol, find_webull_window, load_config,
                       locate_l2_region, ocr_book, preprocess)

OUT = HERE / "test_locate_result.txt"


def main():
    lines = []

    def say(msg):
        print(msg)
        lines.append(msg)

    load_config()
    say(f"time: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    # 1. find the Webull window (by process, not title)
    rect = find_webull_window()
    if rect is None:
        say("STEP 1 FAIL: no visible Webull.exe window found. "
            "Is Webull Desktop running and not minimized?")
        OUT.write_text("\n".join(lines))
        return
    say(f"STEP 1 OK: Webull window at {rect}")

    with mss.MSS() as sct:
        img = np.asarray(sct.grab(rect))

        # 2. locate the L2 header inside the window
        t0 = time.time()
        region = locate_l2_region(img, rect, HERE)
        dt = time.time() - t0
        dbg = np.ascontiguousarray(img[:, :, :3])
        if region:
            x, y = region["left"] - rect["left"], region["top"] - rect["top"]
            cv2.rectangle(dbg, (x, y),
                          (x + region["width"], y + region["height"]),
                          (0, 255, 0), 3)
            say(f"STEP 2 OK: L2 panel located in {dt:.2f}s -> {region}")
        else:
            say(f"STEP 2 FAIL ({dt:.2f}s): header not found - see "
                "debug_words.txt for what OCR saw. Green box missing in "
                "test_window.png.")
        cv2.imwrite(str(HERE / "test_window.png"), dbg)

        # 3. symbol detection
        sym = detect_symbol(img)
        say(f"STEP 3 {'OK' if sym else 'FAIL'}: symbol = {sym}")

        if region:
            # 4. read the book from the located region, 5 attempts
            cv2.imwrite(str(HERE / "test_region.png"),
                        np.ascontiguousarray(
                            np.asarray(sct.grab(region))[:, :, :3]))
            cv2.imwrite(str(HERE / "test_region_th.png"),
                        preprocess(np.asarray(sct.grab(region))))
            ok = 0
            book = None
            for i in range(5):
                b = ocr_book(sct, region)
                if b:
                    ok += 1
                    book = b
                time.sleep(0.2)
            if book:
                say(f"STEP 4 OK: {ok}/5 reads parsed. Last: "
                    f"{len(book.bids)} levels, bid {book.best_bid:.3f} / "
                    f"ask {book.best_ask:.3f}, spread {book.spread_pct:.2f}%, "
                    f"imbalance {book.imbalance:.2f}")
            else:
                say("STEP 4 FAIL: region found but 0/5 reads parsed - "
                    "see test_region.png / test_region_th.png")

    say("files written: test_window.png, test_region.png, "
        "test_region_th.png, test_locate_result.txt")
    OUT.write_text("\n".join(lines))


if __name__ == "__main__":
    main()
