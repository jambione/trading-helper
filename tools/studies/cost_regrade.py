"""Re-grade the frozen published means in docs/studies/cost_regrade_prereg.json.

No market data. `--score` prints the verdicts. It does not read a warehouse.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PREREG = ROOT / "docs" / "studies" / "cost_regrade_prereg.json"
COST = 3.0


def norm_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def norm_isf(p: float) -> float:
    if not 0.0 < p < 1.0:
        raise ValueError(p)
    lo, hi = -20.0, 20.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if norm_sf(mid) > p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def bonferroni_t(single_t: float, k: int) -> float:
    if k < 1:
        raise ValueError(k)
    return norm_isf(norm_sf(single_t) / k)


def net3_of(kind: str, mean_bp: float, cost_bp: float | None) -> float:
    if kind == "gross":
        return mean_bp - COST
    if kind == "net":
        if cost_bp is None:
            raise ValueError("net candidate needs cost_bp")
        return (mean_bp + cost_bp) - COST
    if kind == "realized":
        return mean_bp
    raise ValueError(kind)


def t_net(published_t, published_mean, net3) -> float | None:
    if published_t is None or published_mean in (None, 0):
        return None
    return float(published_t) * (net3 / float(published_mean))


def grade_family(doc: dict) -> list[dict]:
    graded = [c for c in doc["candidates"] if c.get("kind")]
    k = len(graded)
    t_pass = bonferroni_t(2.0, k)
    t_prom = bonferroni_t(1.5, k)
    rows = []
    for c in doc["candidates"]:
        kind = c.get("kind")
        if kind is None:
            rows.append({**c, "verdict": "NOT_SCORED", "k": k})
            continue
        if kind == "realized":
            net = net3_of("realized", c["realized_bp"], None)
            rows.append({
                "id": c["id"],
                "net3": [net],
                "verdict": "FAIL" if net <= 0 else "UNSPLIT",
                "k": k,
            })
            continue
        nets = []
        ts = []
        for h in c["halves"]:
            n3 = net3_of(kind, h["mean_bp"], c.get("cost_bp"))
            nets.append(n3)
            ts.append(t_net(h.get("t"), h["mean_bp"], n3))
        if any(n <= 0 for n in nets):
            verdict = "FAIL"
        elif any(t is None or t < t_pass for t in ts):
            verdict = "FAIL"
        else:
            verdict = "PASS"
        # PROMISING is only the explicit pooled object. Per-half t cannot promote.
        pooled = c.get("pooled") if isinstance(c.get("pooled"), dict) else None
        if (
            verdict == "FAIL"
            and all(n > 0 for n in nets)
            and pooled is not None
            and pooled.get("mean_bp") is not None
            and pooled.get("t") is not None
        ):
            p_net = net3_of(kind, pooled["mean_bp"], c.get("cost_bp"))
            p_t = t_net(pooled["t"], pooled["mean_bp"], p_net)
            if p_net > 0 and p_t is not None and p_t >= t_prom:
                verdict = "PROMISING"
        rows.append({
            "id": c["id"],
            "net3": nets,
            "t_net": ts,
            "t_pass": t_pass,
            "t_prom": t_prom,
            "verdict": verdict,
            "k": k,
        })
    return rows


def main() -> None:
    if "--score" not in sys.argv:
        raise SystemExit("refusing to print verdicts without --score")
    doc = json.loads(PREREG.read_text())
    rows = grade_family(doc)
    print(f"K {rows[0]['k'] if rows else 0} cost {COST:.0f} bp")
    for row in rows:
        print(f"{row['id']}: {row['verdict']} net3 {row.get('net3')}")


if __name__ == "__main__":
    main()
