"""ai_watch_engine_push_count: 'all' (default) is the live count; 'book' counts src=book engine names + in-flight."""
import time

import ai_entry_watch as ew

ROWS = [{"ticker": "AAA", "src": "book"}, {"ticker": "BBB", "src": "book"},
        {"ticker": "MOM", "src": "momentum"}, {"ticker": "DSC", "src": "discord"}]


def test_all_mode_counts_every_known_name(monkeypatch):
    monkeypatch.setattr(ew, "_push_cfg", lambda: {})
    monkeypatch.setattr(ew, "_dashboard_tickers", lambda: ROWS)
    assert ew._push_cap_used({"AAA", "BBB", "MOM", "DSC"}) == 4
    monkeypatch.setattr(ew, "_push_cfg", lambda: {"ai_watch_engine_push_count": "all"})
    assert ew._push_cap_used({"AAA", "MOM"}) == 2


def test_book_mode_counts_book_names_and_in_flight_pushes(monkeypatch):
    monkeypatch.setattr(ew, "_push_cfg", lambda: {"ai_watch_engine_push_count": "book", "scan_interval_sec": 60})
    monkeypatch.setattr(ew, "_dashboard_tickers", lambda: ROWS)
    now = time.monotonic()
    monkeypatch.setattr(ew, "_pushed_at", {"NEW": now - 10.0, "OLD": now - 500.0, "AAA": now - 5.0})
    # known: AAA, BBB (book), MOM, DSC (not book); in flight: NEW (not known, 10 s ago); OLD expired; AAA already known
    assert ew._push_cap_used({"AAA", "BBB", "MOM", "DSC"}) == 3
