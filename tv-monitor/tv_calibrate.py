"""Calibrate the four TradingView indicator panels, in order:

    1. STAR   (0..100 oscillator)      - drag over the full panel height
    2. HEART  (0..-100 exhaustion)     - full panel height
    3. CHECK  (MACD)                   - full panel height
    4. FIRE   (LazyBear squeeze momentum) - full panel height

Drag each region; the overlay tells you which one is next. Esc skips a
panel, closing the window cancels. Regions save to tv_config.json.

IMPORTANT: drag the PLOT AREA only (not the price axis on the right),
and full panel height so values map to the right scale.
"""
import json
import sys
import tkinter as tk
from pathlib import Path

if sys.platform == "win32":
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

CONFIG_PATH = Path(__file__).parent / "tv_config.json"
PANELS = ["star", "heart", "check", "fire"]


def virtual_screen():
    if sys.platform == "win32":
        u = ctypes.windll.user32
        return (u.GetSystemMetrics(76), u.GetSystemMetrics(77),
                u.GetSystemMetrics(78), u.GetSystemMetrics(79))
    return (0, 0, 1920, 1080)


def main():
    root = tk.Tk()
    vx, vy, vw, vh = virtual_screen()
    root.overrideredirect(True)
    root.geometry(f"{vw}x{vh}+{vx}+{vy}")
    root.attributes("-alpha", 0.25)
    root.attributes("-topmost", True)

    canvas = tk.Canvas(root, cursor="cross", bg="grey11",
                       highlightthickness=0)
    canvas.pack(fill=tk.BOTH, expand=True)
    label = canvas.create_text(vw // 2, 60, fill="white",
                               font=("Segoe UI", 20), text="")
    regions: dict = {}
    state = {"i": 0, "x0": 0, "y0": 0, "rect": None}

    def prompt():
        if state["i"] >= len(PANELS):
            finish()
            return
        canvas.itemconfigure(
            label, text=f"Drag over the {PANELS[state['i']].upper()} panel "
                        f"({state['i'] + 1}/{len(PANELS)}) — plot area only, "
                        "full height. Esc = skip this panel.")

    def finish():
        root.destroy()
        cfg = (json.loads(CONFIG_PATH.read_text())
               if CONFIG_PATH.exists() else {})
        cfg["panels"] = regions
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
        print(f"Saved {len(regions)} panel regions to {CONFIG_PATH}:")
        for k, v in regions.items():
            print(f"  {k}: {v}")

    def press(e):
        state["x0"], state["y0"] = e.x, e.y
        state["rect"] = canvas.create_rectangle(e.x, e.y, e.x, e.y,
                                                outline="lime", width=2)

    def drag(e):
        canvas.coords(state["rect"], state["x0"], state["y0"], e.x, e.y)

    def release(e):
        left, top = min(state["x0"], e.x), min(state["y0"], e.y)
        w, h = abs(e.x - state["x0"]), abs(e.y - state["y0"])
        if w >= 20 and h >= 15:
            regions[PANELS[state["i"]]] = {
                "left": vx + left, "top": vy + top, "width": w, "height": h}
            canvas.itemconfigure(state["rect"], outline="cyan")
            state["i"] += 1
        prompt()

    def skip(_):
        state["i"] += 1
        prompt()

    canvas.bind("<ButtonPress-1>", press)
    canvas.bind("<B1-Motion>", drag)
    canvas.bind("<ButtonRelease-1>", release)
    root.bind("<Escape>", skip)
    prompt()
    root.mainloop()


if __name__ == "__main__":
    main()
