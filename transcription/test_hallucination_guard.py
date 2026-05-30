"""
test_hallucination_guard.py — regression tests for is_hallucination + the
greedy-segmentation cap. Both protect against Whisper loop output spraying
1x false-positive tickers (diagnosed on Ticker4.m4a). ASR-free, runs instantly.

    venv/bin/python -m pytest transcription/test_hallucination_guard.py -q
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ticker_extract import is_hallucination, extract_tickers   # noqa: E402


def _tk(text):
    return set(extract_tickers(text).keys())


# ── Loops / hallucinations that must be flagged ───────────────────────────────

def test_dotted_letter_loop_flagged():
    assert is_hallucination("B.I.Y.Y.Y.Y.Y.Y.Y.Y.Y.Y.Y")


def test_letter_dominated_loop_flagged():
    assert is_hallucination("E-I-Y-A-I-I-A-I-I-I-I-I-I-I-I-I-I-I-I")


def test_short_unit_repeated_flagged():
    assert is_hallucination("xx " * 10)


def test_unknown_token_repeated_flagged():
    assert is_hallucination("zzz " * 8)


def test_real_ticker_spammed_is_a_loop():
    assert is_hallucination("B-I-Y-A " * 60)          # 60x = loop


def test_prompt_echo_dump_flagged():
    assert is_hallucination(
        "AAPL META FILEC TTTT TTTT TTTT TTTT TTTT TTTT TTTT TTTT TTTT TTTT"
    )


# ── Real content that must NOT be flagged ─────────────────────────────────────

def test_real_ticker_spelled_normally_passes():
    # BIYA spelled ~30x in a burst is a real, intentional mention — keep it.
    assert not is_hallucination("B-I-Y-A " * 31)


def test_normal_speech_passes():
    assert not is_hallucination("the stock looks strong into the close today")


def test_clean_tickers_pass():
    assert not is_hallucination("NVDA and AMD both ripping")
    assert _tk("NVDA and AMD both ripping") == {"NVDA", "AMD"}


# ── Greedy-segmentation cap ───────────────────────────────────────────────────

def test_long_letter_run_yields_no_tickers():
    # A 15-letter single-letter run is a hallucination — must not be carved up.
    assert _tk("A Z O F A Z U L O H O N G O N G") == set()


def test_short_spell_still_segments():
    assert _tk("H T T B I Y A") == {"HTT", "BIYA"}
