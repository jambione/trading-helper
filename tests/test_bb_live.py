"""
test_bb_live.py — offline tests for the "Bullish Bob LIVE" call-out path.

Covers parse_bb_live (author detection, badge stripping, and the deliberately
strict ticker gate) plus the dashboard side that turns those calls into the
header's "Suggests:" chip and its history list.

The gate is the point of most of these: a symbol shown under the product badge
is read as an idea to act on, so anything we can't confirm must serve NO symbol
rather than a guess.

Run:
    venv/bin/python -m pytest tests/test_bb_live.py -q
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord_source as ds   # noqa: E402


# ── parse_bb_live: the confident cases ───────────────────────────────────────

def test_plain_call_out():
    tkr, text = ds.parse_bb_live("Bullish Bob LIVE 🟢 🍏BULL NRXP pop")
    assert tkr == "NRXP"
    assert text == "NRXP pop"


def test_call_out_with_timestamp_gutter():
    # Discord's compact view puts the timestamp on the same OCR row.
    tkr, _ = ds.parse_bb_live("[9:32 AM] Bullish Bob LIVE 🟢 🍏BULL NAMI vol")
    assert tkr == "NAMI"


def test_ocr_dropped_the_emoji():
    # Vision often drops the coloured dot entirely; only the badge word remains.
    tkr, _ = ds.parse_bb_live("9:41 AM Bullish Bob LIVE BULL NRXP low vol")
    assert tkr == "NRXP"


def test_dollar_prefixed_symbol():
    tkr, text = ds.parse_bb_live("Bullish Bob LIVE 🟢 🍏BULL $NAMI running")
    assert tkr == "NAMI"
    assert text == "$NAMI running"


def test_trailing_punctuation_tolerated():
    tkr, _ = ds.parse_bb_live("Bullish Bob LIVE 🟢 🍏BULL NRXP, watching this")
    assert tkr == "NRXP"


# ── parse_bb_live: "unsure" serves no symbol ─────────────────────────────────

def test_lowercase_lead_serves_no_symbol():
    # "nami" is a real symbol in lower case — but in prose we can't tell a call
    # from a word, so no suggestion is served. The text still comes back.
    tkr, text = ds.parse_bb_live("Bullish Bob LIVE 🟢 🍏BULL nami test res")
    assert tkr is None
    assert text == "nami test res"


def test_mixed_case_lead_serves_no_symbol():
    tkr, text = ds.parse_bb_live("Bullish Bob LIVE 🟢 🍏BULL Nami looks good")
    assert tkr is None
    assert text == "Nami looks good"


def test_unlisted_symbol_serves_no_symbol():
    tkr, text = ds.parse_bb_live("Bullish Bob LIVE 🟢 🍏BULL ZZZQQ ripping")
    assert tkr is None
    assert text == "ZZZQQ ripping"


def test_trade_action_word_serves_no_symbol():
    # "ALL" is Allstate and "ON" is ON Semiconductor — both far likelier to be
    # the caller talking than the caller naming that stock.
    for line in ("Bullish Bob LIVE 🟢 🍏BULL ALL out here",
                 "Bullish Bob LIVE 🟢 🍏BULL ON the bid"):
        tkr, text = ds.parse_bb_live(line)
        assert tkr is None, line
        assert text


def test_chat_abbreviation_serves_no_symbol():
    tkr, _ = ds.parse_bb_live("Bullish Bob LIVE 🟢 🍏BULL HOD break coming")
    assert tkr is None


def test_sentence_serves_no_symbol():
    tkr, text = ds.parse_bb_live("Bullish Bob LIVE 🟢 🍏BULL watching the tape")
    assert tkr is None
    assert text == "watching the tape"


def test_empty_message_is_not_a_call_out():
    assert ds.parse_bb_live("Bullish Bob LIVE 🟢 🍏BULL") == (None, "")


# ── parse_bb_live: lines that aren't call-outs at all ────────────────────────

def test_other_author_ignored():
    assert ds.parse_bb_live("[9:40 AM]Joosshhhh [BULL]: NRXP looks good") == (None, "")


def test_bullish_bob_without_live_ignored():
    # The channel title and the plain (non-LIVE) account are not the caller.
    assert ds.parse_bb_live("Bullish Bob's Trading Hub") == (None, "")


def test_scanner_alert_ignored():
    line = "INHD Price Volatility Spike! >>>>> 1 Minute High Price = 41.83"
    assert ds.parse_bb_live(line) == (None, "")


def test_call_out_is_not_mistaken_for_an_alert():
    # The two parsers must not both claim the same line.
    line = "Bullish Bob LIVE 🟢 🍏BULL NRXP pop"
    assert ds.parse_alert_line(line)[0] is None
    assert ds.parse_chat_sentiment(line)[0] is None


# ── parse_bb_live_lines: the shapes Vision actually produces ─────────────────
# Verbatim rows from a live capture. Vision sorts by vertical midpoint, so the
# author's name and its message land on separate rows in an unpredictable order
# — the reason a whole-frame parser exists at all.

def test_frame_body_row_above_its_author():
    calls, used = ds.parse_bb_live_lines([
        "* BULL sold azi lotto - loss",
        "8:21AM Bullish Bob LIVE",
    ])
    assert calls == [(None, "sold azi lotto - loss", "8:21 AM")]
    assert used == {0, 1}


def test_frame_body_row_below_its_author():
    calls, _ = ds.parse_bb_live_lines([
        "Bullish Bob LIVE",
        "• BULL NAMI vol",
    ])
    assert calls == [("NAMI", "NAMI vol", "")]


def test_frame_single_row_with_glyph_noise():
    # The 🟢 and 🍏 badges come back as stray glyphs on the author's own row.
    calls, _ = ds.parse_bb_live_lines(["8:28 AM Bullish Bob LIVE• Ó BULL NRXP pop"])
    assert calls == [("NRXP", "NRXP pop", "8:28 AM")]


def test_frame_author_row_with_glyph_only_still_pairs():
    # "Bullish Bob LIVE O" is a bare author row — the "O" is the 🟢 badge, not
    # a message, so it must not block pairing with the body row.
    calls, _ = ds.parse_bb_live_lines([
        "Bullish Bob LIVE O",
        "•BULL NRXP low vol",
    ])
    assert calls == [("NRXP", "NRXP low vol", "")]


def test_frame_badge_row_with_no_author_nearby_is_ignored():
    # Without an adjacent author row we can't attribute the text to this caller.
    calls, used = ds.parse_bb_live_lines([
        "BULL FLAG setup on the daily",
        "some other line",
        "Bullish Bob LIVE",
    ])
    assert calls == []
    assert 0 not in used


def test_frame_real_capture_sidebar_interleaved():
    # The Discord channel sidebar interleaves with the message column; only the
    # call-outs may be claimed.
    lines = [
        "* BULL sold azi lotto - loss",
        "8:21AM Bullish Bob LIVE",
        "-read-first",
        "*BULL watching",
        "Bullish Bob LIVE",
        "# an-get-help",
        "•BULL flat on the day",
        "Bullish Bob LIVE",
        "?-live-support",
        "Bullish Bob LIVE",
        "• BULL NAMI vol",
        "NAMI New Daily High >>>>> Current Price = 9.00",
        "8:28 AM Bullish Bob LIVE• Ó BULL NRXP pop",
        "•BULL low vol",
        "Bullish Bob LIVE",
    ]
    calls, used = ds.parse_bb_live_lines(lines)
    assert [c[0] for c in calls] == [None, None, None, "NAMI", "NRXP", None]
    # The scanner alert and the sidebar rows stay available to the other parsers.
    assert 11 not in used and 2 not in used and 5 not in used


def test_frame_ignores_a_frame_with_no_call_outs():
    assert ds.parse_bb_live_lines(["SPY NEW WEEKLY LOW >>>>> Price: $739.20"]) == ([], set())


def test_the_name_inside_the_channel_blurb_is_not_an_author_row():
    """Verbatim from a capture. The caller's name appears mid-sentence in the
    channel description, and treating that as an author row would let any badge
    line beside it be attributed to him."""
    lines = [
        "LIVE Trading @ 7:00am Eastern With @Bullish Bob LIVE",
        "•BULL NRXP pop",
    ]
    calls, used = ds.parse_bb_live_lines(lines)
    assert calls == []
    assert used == set()


# ── the message's own timestamp ──────────────────────────────────────────────
# Capture time is when we looked at the screen, which on a fresh start stamps an
# hour of call-outs with one minute. Discord's own stamp is the real answer.

def test_said_time_normalised_from_the_author_row():
    for row, want in (("8:21AM Bullish Bob LIVE",       "8:21 AM"),
                      ("[9:32 AM] Bullish Bob LIVE",    "9:32 AM"),
                      ("12:05 PM Bullish Bob LIVE",     "12:05 PM"),
                      ("Bullish Bob LIVE",              "")):
        calls, _ = ds.parse_bb_live_lines([row, "•BULL NRXP pop"])
        assert calls[0][2] == want, row


def test_a_time_inside_the_message_is_not_the_said_time():
    # "out by 3:45 PM" is the caller talking, not when he said it.
    calls, _ = ds.parse_bb_live_lines(["Bullish Bob LIVE 🟢 🍏BULL NRXP out by 3:45 PM"])
    assert calls == [("NRXP", "NRXP out by 3:45 PM", "")]


def test_split_call_out_borrows_its_authors_time():
    calls, _ = ds.parse_bb_live_lines(["•BULL NAMI vol", "8:21AM Bullish Bob LIVE"])
    assert calls == [("NAMI", "NAMI vol", "8:21 AM")]


def test_said_time_drives_display_and_freshness():
    """A call read at noon but stamped 8:21 AM is old, and must say so."""
    dash = _fresh_dashboard()
    now  = time.time()
    dash.ingest_discord_alerts([], [], {}, [
        {"ticker": "NRXP", "text": "NRXP pop", "ts": now, "said": "8:21 AM"},
    ])
    snap = dash.bb_live_snapshot(now)
    rec  = snap["history"][0]
    assert rec["said"] == "8:21 AM"
    # Read just now, but said hours ago → not the current suggestion.
    assert rec["at"] < now - dash._BB_LIVE_FRESH_SEC
    assert snap["current"] is None


def test_missing_said_falls_back_to_capture_time():
    dash = _fresh_dashboard()
    now  = time.time()
    dash.ingest_discord_alerts([], [], {}, [
        {"ticker": "NRXP", "text": "NRXP pop", "ts": now, "said": ""},
    ])
    snap = dash.bb_live_snapshot(now)
    assert snap["current"]["ticker"] == "NRXP"
    assert snap["current"]["at"] == snap["current"]["unix"]


def test_a_said_time_in_the_future_falls_back_to_capture_time():
    # A stamp we can't place on today's clock (yesterday's message still on
    # screen, or an OCR misread) must not invent a call that hasn't happened.
    dash = _fresh_dashboard()
    now  = time.time()
    ahead = dash.datetime.fromtimestamp(now, dash.ET).replace(
        hour=23, minute=59, second=0, microsecond=0)
    if ahead.timestamp() <= now + 120:
        return   # running late enough in the day that 23:59 isn't future
    dash.ingest_discord_alerts([], [], {}, [
        {"ticker": "NRXP", "text": "NRXP pop", "ts": now, "said": "11:59 PM"},
    ])
    rec = dash.bb_live_snapshot(now)["history"][0]
    assert rec["at"] == rec["unix"]


def test_history_is_ordered_by_when_it_was_said():
    """Priming reads a whole screen at once, so arrival order proves nothing."""
    dash = _fresh_dashboard()
    now  = time.time()
    et   = dash.datetime.fromtimestamp(now, dash.ET)
    if et.hour < 11:
        return   # need three past times on today's clock
    dash.ingest_discord_alerts([], [], {}, [
        {"ticker": "AAA", "text": "AAA x", "ts": now, "said": "10:30 AM"},
        {"ticker": "BBB", "text": "BBB x", "ts": now, "said": "8:15 AM"},
        {"ticker": "CCC", "text": "CCC x", "ts": now, "said": "9:45 AM"},
    ])
    order = [c["ticker"] for c in dash.bb_live_snapshot(now)["history"]]
    assert order == ["AAA", "CCC", "BBB"]


# ── dashboard: ingest → "Suggests:" chip + history ───────────────────────────

def _fresh_dashboard():
    import dashboard as dash
    dash.STATE.bb_live.clear()
    return dash


def test_ingest_populates_current_and_history():
    dash = _fresh_dashboard()
    now  = time.time()
    dash.ingest_discord_alerts([], [], {}, [
        {"ticker": "NAMI", "text": "NAMI vol",  "ts": now - 60},
        {"ticker": "NRXP", "text": "NRXP pop",  "ts": now},
    ])
    snap = dash.bb_live_snapshot(now)
    assert snap["current"]["ticker"] == "NRXP"
    assert [c["ticker"] for c in snap["history"]] == ["NRXP", "NAMI"]


def test_repeat_call_moves_to_current_without_duplicating():
    dash = _fresh_dashboard()
    now  = time.time()
    dash.ingest_discord_alerts([], [], {}, [
        {"ticker": "NAMI", "text": "NAMI vol", "ts": now - 120},
        {"ticker": "NRXP", "text": "NRXP pop", "ts": now - 60},
        {"ticker": "NAMI", "text": "NAMI again", "ts": now},
    ])
    snap = dash.bb_live_snapshot(now)
    assert snap["current"]["ticker"] == "NAMI"
    assert [c["ticker"] for c in snap["history"]] == ["NAMI", "NRXP"]
    assert snap["history"][0]["text"] == "NAMI again"


def test_stale_call_drops_off_the_chip_but_stays_in_history():
    dash = _fresh_dashboard()
    now  = time.time()
    old  = now - dash._BB_LIVE_FRESH_SEC - 1
    dash.ingest_discord_alerts([], [], {}, [{"ticker": "NRXP", "text": "NRXP pop", "ts": old}])
    snap = dash.bb_live_snapshot(now)
    assert snap["current"] is None
    assert [c["ticker"] for c in snap["history"]] == ["NRXP"]


def test_malformed_symbol_is_refused_server_side():
    dash = _fresh_dashboard()
    dash.ingest_discord_alerts([], [], {}, [
        {"ticker": "TOOLONG", "text": "x"},
        {"ticker": "N4MI",    "text": "x"},
        {"ticker": "",        "text": "x"},
        "not a dict",
        {"ticker": "NRXP", "text": "ok", "ts": "garbage"},
    ])
    assert [c["ticker"] for c in dash.bb_live_snapshot()["history"]] == ["NRXP"]


# ── the on-disk archive ──────────────────────────────────────────────────────
# The header chip is a deque that empties on every dashboard restart. Without
# this file the desk keeps no record of its most prominent signal.

def _read_archive(dash) -> list[dict]:
    import ai_paths
    p = ai_paths.report_file("bb_live.jsonl")
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]


def _clear_archive():
    import ai_paths
    p = ai_paths.report_file("bb_live.jsonl")
    if p.exists():
        p.unlink()


def test_call_out_is_archived_to_disk():
    dash = _fresh_dashboard()
    _clear_archive()
    now = time.time()
    dash.ingest_discord_alerts([], [], {}, [
        {"ticker": "NRXP", "text": "NRXP pop", "ts": now, "said": "8:21 AM"},
    ])
    rows = _read_archive(dash)
    assert len(rows) == 1
    assert rows[0]["ticker"] == "NRXP"
    assert rows[0]["text"] == "NRXP pop"
    assert rows[0]["said"] == "8:21 AM"
    assert "at" in rows[0] and "time" in rows[0]


def test_archive_survives_a_state_reset():
    """The point of the file: memory is cleared, the record is not."""
    dash = _fresh_dashboard()
    _clear_archive()
    dash.ingest_discord_alerts([], [], {}, [{"ticker": "NAMI", "text": "NAMI vol"}])
    dash.STATE.bb_live.clear()                       # simulate a restart
    assert dash.bb_live_snapshot()["history"] == []
    assert [r["ticker"] for r in _read_archive(dash)] == ["NAMI"]


def test_replayed_call_out_is_not_archived_twice():
    """The OCR source re-posts everything visible when it restarts."""
    dash = _fresh_dashboard()
    _clear_archive()
    call = {"ticker": "NRXP", "text": "NRXP pop", "said": "8:21 AM"}
    dash.ingest_discord_alerts([], [], {}, [dict(call)])
    dash.ingest_discord_alerts([], [], {}, [dict(call)])
    assert len(_read_archive(dash)) == 1
    # A genuinely new call on the same symbol IS a second row.
    dash.ingest_discord_alerts([], [], {},
                               [{"ticker": "NRXP", "text": "NRXP again", "said": "9:02 AM"}])
    assert len(_read_archive(dash)) == 2


def test_archive_records_the_price_and_its_source():
    dash = _fresh_dashboard()
    _clear_archive()
    now = time.time()
    with dash.STATE.lock:
        dash.STATE.tickers["AAA"] = {"price": 4.20}
        # OTC name the quote feed has nothing for — only the scanner's seed.
        dash.STATE.tickers["BBB"] = {"scanner_price": 1.75,
                                     "scanner_price_ts": now - 30}
    dash.ingest_discord_alerts([], [], {}, [
        {"ticker": "AAA", "text": "AAA pop"},
        {"ticker": "BBB", "text": "BBB pop"},
        {"ticker": "CCC", "text": "CCC pop"},      # nothing known at all
    ])
    by = {r["ticker"]: r for r in _read_archive(dash)}
    assert (by["AAA"]["price"], by["AAA"]["price_src"]) == (4.20, "quote")
    assert (by["BBB"]["price"], by["BBB"]["price_src"]) == (1.75, "scanner")
    assert by["BBB"]["price_age_sec"] >= 30
    assert (by["CCC"]["price"], by["CCC"]["price_src"]) == (None, None)


def test_archive_honours_the_test_report_dir():
    """conftest redirects AI_REPORT_DIR; a suite run must never append to the
    real ai_reports/bb_live.jsonl mid-session."""
    import ai_paths
    assert "ai_reports_test_" in str(ai_paths.report_file("bb_live.jsonl"))


def test_call_outs_never_touch_mentions():
    # A caller's chatter must not be able to move the burst/trader path.
    dash = _fresh_dashboard()
    before = dict(dash.STATE.mention_daily)
    dash.ingest_discord_alerts([], [], {}, [{"ticker": "NRXP", "text": "NRXP pop"}])
    assert dash.STATE.mention_daily == before


def test_ingest_endpoint_accepts_bb_live():
    """The producer's POST body reaches STATE — the seam the OCR source uses."""
    from fastapi.testclient import TestClient

    dash = _fresh_dashboard()
    with TestClient(dash.app) as client:
        r = client.post("/api/discord/ingest", json={
            "alerts": [],
            "bb_live": [{"ticker": "NRXP", "text": "NRXP pop", "ts": time.time()}],
        })
    assert r.status_code == 200 and r.json()["ok"] is True
    assert dash.bb_live_snapshot()["current"]["ticker"] == "NRXP"


def test_ingest_endpoint_survives_a_malformed_bb_live_field():
    from fastapi.testclient import TestClient

    dash = _fresh_dashboard()
    with TestClient(dash.app) as client:
        r = client.post("/api/discord/ingest", json={"alerts": [], "bb_live": "nope"})
    assert r.status_code == 200
    assert dash.bb_live_snapshot()["history"] == []
