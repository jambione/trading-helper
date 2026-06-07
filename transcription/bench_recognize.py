#!/usr/bin/env python3
"""
bench_recognize.py — unit benchmark for STAGE 2 (recognize()), pure logic, no
audio, runs in milliseconds. This is the loop where recognition rules are
EARNED: add a rule to spell_pipeline.recognize only when it turns a failing
case green without breaking a passing one.

Cases are written in the exact form Stage-1 ASR tends to EMIT for each spoken
style (bare token / spaced letters / hyphens / NATO words / letter-names),
drawn from the real sample clips plus the classic spelling patterns.

Run:  venv/bin/python transcription/bench_recognize.py
      venv/bin/python transcription/bench_recognize.py -v
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
from spell_pipeline import recognize

# (asr_emits, expected_tickers, note)
CASES: list[tuple[str, set, str]] = [
    # --- bare symbol tokens: the common scanner case ----------------------
    ("ELPW ELPW ELPW ELPW",                {"ELPW"},          "bare token, repeated"),
    ("MCRP",                               {"MCRP"},          "bare token"),
    ("QCOM up 5 percent",                  {"QCOM"},          "bare token in fragment"),
    ("Ticker GAMB new highs",              {"GAMB"},          "bare token + words"),
    ("ATGL ELPW ECX",                      {"ATGL","ELPW","ECX"}, "three bare tokens"),
    # --- spelled with spaces ---------------------------------------------
    ("N V D A",                            {"NVDA"},          "spaced letters"),
    ("G A M B",                            {"GAMB"},          "spaced letters"),
    ("T S L A",                            {"TSLA"},          "spaced letters"),
    # --- hyphenated -------------------------------------------------------
    ("G-A-M-B",                            {"GAMB"},          "hyphen letters"),
    ("M-E-H-A",                            {"MEHA"},          "hyphen letters"),
    # --- period-separated (Whisper renders spells this way too) -----------
    ("E.L.P.W.",                           {"ELPW"},          "period letters"),
    ("scanner alert E.L.P.W. now",         {"ELPW"},          "period letters in context"),
    ("E.L.P.W. E.L.P.W. E.L.P.W.",         {"ELPW"},          "repeated valid spell (scanner spam)"),
    # --- NATO phonetic ----------------------------------------------------
    ("Quebec Charlie Oscar Michael",       {"QCOM"},          "NATO (Michael=M)"),
    ("Tango Papa Echo Tango",              {"TPET"},          "NATO phonetic"),
    # --- letter-name phonetics -------------------------------------------
    ("en vee dee ay",                      {"NVDA"},          "letter-names"),
    # --- company names spoken as words (price-tape style) ----------------
    ("Meta at 528 down 1.7%",              {"META"},          "company word -> META"),
    ("Apple and Microsoft rallying",       {"AAPL","MSFT"},   "two company words"),
    ("Coinbase up 5.6%",                   {"COIN"},          "Coinbase -> COIN"),
    ("Robinhood up 3.8%",                  {"HOOD"},          "Robinhood -> HOOD"),
    ("Nvidia hit a new high",              {"NVDA"},          "company word -> NVDA"),
    # --- negatives: must stay empty --------------------------------------
    ("the CIA says Iran can outlast",      set(),             "agency, not ticker"),
    ("AI server demand is strong",         set(),             "topic word, not ticker"),
    ("just buying it right now",           set(),             "plain english"),
    ("up 7 percent on the day",            set(),             "plain english"),
    ("he broke his arm yesterday",         set(),             "arm body part, not ARM"),
    ("flip a coin to decide",              set(),             "coin word, not COIN"),
    ("kids in the hood today",             set(),             "hood word, not HOOD"),
    ("raised the price target to 200",     set(),             "price target, not TGT"),
]


# Watchlist fuzzy-correction: (asr, watchlist, expected, note). A mis-heard spell
# snaps to a tracked ticker one edit away; valid tickers are never overridden;
# ambiguous or unbacked cases must NOT snap.
WL_CASES: list[tuple[str, set, set, str]] = [
    ("E.O.P.W.",            {"ELPW"},          {"ELPW"}, "snap mis-heard spell -> watchlist"),
    ("EOPW",                {"ELPW"},          {"ELPW"}, "snap glued mis-hear -> watchlist"),
    ("ticker EOPW now",     {"ELPW"},          {"ELPW"}, "snap in context"),
    ("E.O.P.W. E.O.P.W.",   {"ELPW"},          {"ELPW"}, "repeated period-spell -> snap"),
    ("E O P W E O P W E O P W", {"ELPW"},       {"ELPW"}, "repeated spaced mis-hear -> snap"),
    ("EOPW",                set(),             set(),    "no watchlist -> no snap"),
    ("EOPW",                {"ELPW", "EOPX"},  set(),    "ambiguous (2 within 1 edit) -> no snap"),
    ("AAPL",                {"AAPM"},          {"AAPL"}, "valid ticker never overridden"),
    ("MEHA",                {"ELPW"},          {"MEHA"}, "valid stays, no spurious snap"),
    ("EO",                  {"EL"},            set(),    "too short (<3) to snap"),
]


# Deterministic learned/user corrections: (asr, corrections, expected, note).
# A consistent mis-hear always maps to its ticker, regardless of validity.
CORR_CASES: list[tuple[str, dict, set, str]] = [
    ("EOPW",                 {"EOPW": "ELPW"},   {"ELPW"}, "correction snaps invalid mis-hear"),
    ("scanner alert EOPW",   {"EOPW": "ELPW"},   {"ELPW"}, "correction in context"),
    ("E.O.P.W. E.O.P.W.",    {"EOPW": "ELPW"},   {"ELPW"}, "correction + period/repeat"),
    ("NCRP NCRP NCRP",       {"NCRP": "MCRP"},   {"MCRP"}, "correction on repeated spam"),
    ("EOPW",                 {},                 set(),    "no correction -> dropped (invalid)"),
    ("TGL",                  {"TGL": "ATGL"},    {"ATGL"}, "user can override a valid fragment"),
]


def main() -> None:
    verbose = "-v" in sys.argv
    npass = 0
    print(f"{'result':<6} {'note':<34} {'expected':<22} got")
    print("-" * 86)
    rows = ([(a, recognize(a), e, n) for a, e, n in CASES]
            + [(a, recognize(a, watchlist=w), e, n) for a, w, e, n in WL_CASES]
            + [(a, recognize(a, corrections=c), e, n) for a, c, e, n in CORR_CASES])
    for asr, got, expect, note in rows:
        ok = got == expect
        npass += ok
        if verbose or not ok:
            print(f"{'PASS' if ok else 'FAIL':<6} {note:<34} {str(sorted(expect)):<22} {sorted(got)}")
    total = len(CASES) + len(WL_CASES) + len(CORR_CASES)
    print("-" * 86)
    print(f"STAGE-2 recognize(): {npass}/{total} cases pass  "
          f"({len(CASES)} plain + {len(WL_CASES)} watchlist + {len(CORR_CASES)} corrections)")


if __name__ == "__main__":
    main()
