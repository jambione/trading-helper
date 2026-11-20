import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "studies"))
sys.path.insert(0, str(ROOT / "tools"))

import sentiment_backfill as sb  # noqa: E402


def test_calib_interpolates_and_reports_gap():
    c = sb.Calib([(100.0, 1000), (200.0, 2000)])
    assert c.id_at(150.0) == 1500 and c.id_at(50.0) == 1000 and c.id_at(300.0) == 2000
    assert c.gap(150.0) == 50.0


def test_fetch_event_drops_messages_at_or_after_t(monkeypatch):
    import datetime as dt

    def fake_get2(url):
        mx = int(url.split("max=")[1])
        ids = [i for i in range(mx, mx - 30 * 60, -60) if i > 0]
        return {"messages": [{"id": i, "created_at": dt.datetime.fromtimestamp(i, dt.timezone.utc).isoformat().replace("+00:00", "Z")}
                             for i in ids]}
    monkeypatch.setattr(sb, "get", fake_get2)
    monkeypatch.setattr(sb, "iso_ts", lambda s: dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    cal = sb.Calib([(1_000_000.0, 1_000_000), (2_000_000.0, 2_000_000)])
    msgs, req = sb.fetch_event(cal, 1_500_000.0, "X")
    assert len(msgs) == 30 and all(int(m["id"]) < 1_500_000 for m in msgs)
    assert 1_500_000 - 60 <= max(int(m["id"]) for m in msgs) < 1_500_000   # the latest message strictly before T
