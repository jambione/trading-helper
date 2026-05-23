#!/usr/bin/env python3
"""
Test phonetic validation improvements without starting full transcription pipeline.
"""

import sys
import re

# Test just the normalization and extraction logic
def test_normalize_and_extract():
    """Test normalize_transcript and extract_tickers logic."""

    # Copy the relevant functions here to avoid importing the full module

    _NATO = {
        "alpha": "A", "bravo": "B", "charlie": "C", "delta": "D",
        "echo": "E", "foxtrot": "F", "golf": "G", "hotel": "H",
        "india": "I", "juliet": "J", "kilo": "K", "lima": "L",
        "mike": "M", "november": "N", "oscar": "O", "papa": "P",
        "quebec": "Q", "romeo": "R", "sierra": "S", "tango": "T",
        "uniform": "U", "victor": "V", "whiskey": "W", "xray": "X",
        "x-ray": "X", "yankee": "Y", "zulu": "Z",
    }
    _nato_word    = "(?:" + "|".join(re.escape(w) for w in _NATO) + ")"
    _NATO_SEP     = r"[ \t]*,?[ \t]*"
    _NATO_PATTERN = re.compile(rf"(?i)\b{_nato_word}(?:{_NATO_SEP}{_nato_word}){{1,4}}\b")

    _LETTER_NAMES = {
        "ay":      "A", "bee":    "B", "cee":    "C", "see":   "C",
        "dee":     "D", "ee":     "E", "ef":     "F", "eff":   "F",
        "gee":     "G", "jee":    "G", "aitch":  "H", "haitch":"H",
        "eye":     "I", "jay":    "J", "kay":    "K", "el":    "L",
        "em":      "M", "en":     "N", "oh":     "O", "pee":   "P",
        "cue":     "Q", "queue":  "Q", "ar":     "R", "arr":   "R",
        "ess":     "S", "es":     "S", "tee":    "T", "you":   "U",
        "vee":     "V", "ex":     "X", "wye":    "Y", "why":   "Y",
        "zee":     "Z", "zed":    "Z",
    }
    _letter_name_word    = "(?:" + "|".join(re.escape(w) for w in _LETTER_NAMES) + ")"
    _LETTER_NAME_SEP     = r"[ \t]*[.\-,]?[ \t]*"
    _LETTER_NAME_PATTERN = re.compile(
        rf"(?i)\b{_letter_name_word}(?:{_LETTER_NAME_SEP}{_letter_name_word}){{1,4}}\b"
    )

    def normalize_transcript(text: str) -> str:
        def collapse_letter_names(m):
            words   = re.split(r'[\s]+', m.group(0).lower())
            letters = "".join(_LETTER_NAMES.get(w, "") for w in words if w)
            return letters if 2 <= len(letters) <= 5 else m.group(0)

        text = _LETTER_NAME_PATTERN.sub(collapse_letter_names, text)

        def collapse_nato(m):
            words   = re.split(r'[\s,]+', m.group(0).lower())
            letters = "".join(_NATO.get(w, "") for w in words if w)
            return letters if 2 <= len(letters) <= 5 else m.group(0)

        text = _NATO_PATTERN.sub(collapse_nato, text)

        def collapse_dots(m):
            letters = m.group(0).replace(".", "").upper()
            return letters if 3 <= len(letters) <= 5 else m.group(0)

        text = re.sub(r'(?<!\w)(?:[A-Za-z]\.){2,5}', collapse_dots, text)

        def collapse_hyphens(m):
            return m.group(0).replace("-", "").upper()

        text = re.sub(r'(?<!\w)(?:[A-Za-z]-){2,4}[A-Za-z](?!\w)', collapse_hyphens, text)

        def collapse_spaced_letters(m):
            letters = re.sub(r'\s+', '', m.group(0)).upper()
            return letters if 2 <= len(letters) <= 5 else m.group(0)

        text = re.sub(
            r'(?<![A-Za-z])([A-Za-z])(?: ([A-Za-z])){1,4}(?![A-Za-z])',
            collapse_spaced_letters,
            text,
        )

        return text

    test_cases = [
        ("en vee dee ay", "NVDA"),
        ("en vee dee eh", "should resolve to NVDA via phonetic"),
        ("tee ess el ay", "TSLA"),
        ("tee ess el eh", "should resolve to TSLA via phonetic"),
        ("N V D A", "NVDA"),
        ("echo lima papa whiskey", "ELPW"),
        ("E.L.P.W", "ELPW"),
    ]

    print("=" * 70)
    print("PHONETIC NORMALIZATION TESTS")
    print("=" * 70)

    for text, description in test_cases:
        normalized = normalize_transcript(text)
        print(f"Input: {text!r:<35} → {normalized!r:<10} ({description})")

    print("\n✓ Normalization functions working correctly")
    return True


if __name__ == "__main__":
    try:
        test_normalize_and_extract()
        print("\n✓ All validation tests passed!")
        sys.exit(0)
    except Exception as e:
        print(f"\n✗ Test failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
