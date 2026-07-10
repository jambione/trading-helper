"""Drag a box around the L2 order-book rows (just the Size/Bid/Ask/Size rows,
NOT the column headers) and the region is saved to config.json.

Works across multiple monitors — the overlay spans the whole virtual desktop.

Run: python calibrate.py
"""
import json
import sys
import tkinter as tk
from pathlib import Path

if sys.platform == "win32":
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # match l2_signal.py
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

CONFIG_PATH = Path(__file__).parent / "config.json"


def virtual_screen(root):
    """(x, y, w, h) of the full virtual desktop (all monitors)."""
    if sys.platform == "win32":
        u = ctypes.windll.user32
        return (u.GetSystemMetrics(76), u.GetSystemMetrics(77),   # XVIRTUALSCREEN
                u.GetSystemMetrics(78), u.GetSystemMetrics(79))   # CXVIRTUALSCREEN
    return (0, 0, root.winfo_screenwidth(), root.winfo_screenheight())


def main():
    root = tk.Tk()
    vx, vy, vw, vh = virtual_screen(root)
    root.overrideredirect(True)                 # borderless, spans monitors
    root.geometry(f"{vw}x{vh}+{vx}+{vy}")
    root.attributes("-alpha", 0.25)
    root.attributes("-topmost", True)
    root.configure(bg="black")

    canvas = tk.Canvas(root, cursor="cross", bg="grey11",
                       highlightthickness=0)
    canvas.pack(fill=tk.BOTH, expand=True)
    canvas.create_text(
        vw // 2, 60, fill="white", font=("Segoe UI", 18),
        text="Drag a rectangle over the L2 order-book rows on ANY monitor "
             "(numbers only, no headers). Esc cancels.")

    state = {"x0": 0, "y0": 0, "rect": None}

    def press(e):
        state["x0"], state["y0"] = e.x, e.y
        state["rect"] = canvas.create_rectangle(e.x, e.y, e.x, e.y,
                                                outline="lime", width=2)

    def drag(e):
        canvas.coords(state["rect"], state["x0"], state["y0"], e.x, e.y)

    def release(e):
        left, top = min(state["x0"], e.x), min(state["y0"], e.y)
        w, h = abs(e.x - state["x0"]), abs(e.y - state["y0"])
        root.destroy()
        if w < 20 or h < 20:
            print("Selection too small; nothing saved.")
            return
        cfg = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
        # canvas coords are relative to the overlay -> add virtual origin
        cfg["region"] = {"left": vx + left, "top": vy + top,
                         "width": w, "height": h}
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
        print(f"Saved region {cfg['region']} to {CONFIG_PATH}")

    canvas.bind("<ButtonPress-1>", press)
    canvas.bind("<B1-Motion>", drag)
    canvas.bind("<ButtonRelease-1>", release)
    root.bind("<Escape>", lambda e: root.destroy())
    root.mainloop()


if __name__ == "__main__":
    main()
