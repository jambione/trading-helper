#!/usr/bin/env python3
"""pick_email.py - the daily email of the paper picks (operator 11/14: "send this to me every day").

Sends ~/pick_ledgers/POSITIONS.txt (each holding against the S&P) plus the newest picks and the test's verdict line to
the operator's own address (notify_to in config/secrets.json), through the project's mail settings. Run by
scripts/nightly_picks.sh after the holdings report is refreshed, so it goes out on weekday evenings. One email a day.

    .venv/bin/python tools/pick_email.py            # send today's (does nothing if already sent today)
    .venv/bin/python tools/pick_email.py --print    # show the email, send nothing
    .venv/bin/python tools/pick_email.py --force    # send even if one already went out today
"""
from __future__ import annotations

import html
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import auto_picker as A  # noqa: E402
import pick_ledger as P  # noqa: E402


def newest_picks(name: str) -> str:
    rows = P.read_ledger(A.ledger_path(name))
    if not rows:
        return "none yet"
    day = max(r["recorded_at"][:10] for r in rows)
    return f"{', '.join(r['symbol'] for r in rows if r['recorded_at'][:10] == day)} (recorded {day})"


def verdict_lines(report: str) -> list[str]:
    i = report.find("VERDICT at")
    return [x for x in report[i:].splitlines()[:3] if x.strip()] if i >= 0 else []


def subject_for(positions: str, day: str) -> str:
    m = re.search(r"OPEN \((\d+)\).*?(\d+) of \d+ ahead of the S&P; average vs S&P\s+([+-][\d.]+%)", positions, flags=re.DOTALL)
    v = re.search(r"Paper account value: (\$[\d,.]+)", positions)
    bits = [f"Paper picks {day}"]
    if m:
        bits.append(f"{m.group(2)} of {m.group(1)} ahead of S&P ({m.group(3)})")
    if v:
        bits.append(f"account {v.group(1)}")
    return " | ".join(bits)


def build(positions: str, report: str, picks: dict[str, str], day: str) -> tuple[str, str]:
    body = [positions.strip(), "", "NEWEST PICKS", f"  fact-sheet picks (bought on paper): {picks['facts']}",
            f"  lowest scores (not bought):         {picks['facts_avoid']}", f"  random from the same sheet:         {picks['facts_random']}", ""]
    v = verdict_lines(report)
    if v:
        body += ["THE TEST", *[f"  {x}" for x in v], ""]
    body.append("Paper only: no real money. Positions are sold automatically 21 trading days after they are bought.")
    return subject_for(positions, day), "\n".join(body)


def main(argv: list[str]) -> int:
    day = datetime.now(A.ET).strftime("%Y-%m-%d")
    pos_f, rep_f, stamp = A.HOME / "POSITIONS.txt", A.HOME / "FACTS_REPORT.txt", A.HOME / ".last_email"
    if not pos_f.exists():
        print("no holdings report yet; nothing sent")
        return 1
    subject, text = build(pos_f.read_text(), rep_f.read_text() if rep_f.exists() else "",
                          {k: newest_picks(k) for k in ("facts", "facts_avoid", "facts_random")}, day)
    if "--print" in argv:
        print(subject + "\n\n" + text)
        return 0
    if stamp.exists() and stamp.read_text().strip() == day and "--force" not in argv:
        print("already sent today")
        return 0
    import email_service as E
    page = f'<pre style="font-family:Menlo,Consolas,monospace;font-size:13px;line-height:1.45">{html.escape(text)}</pre>'
    ok = E._compose(E._notify_to(), subject, text, page, "daily picks email")
    if ok:
        stamp.write_text(day + "\n")
    print("sent" if ok else "NOT sent (mail settings or server refused; see the log)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
