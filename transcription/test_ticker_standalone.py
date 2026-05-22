#!/usr/bin/env python3
"""
Test ticker recognition improvements - standalone version.

This extracts the key functions to test without initializing audio.
"""

import re


# ========================= NATO & NORMALIZATION =========================

_NATO = {
    "alpha": "A", "bravo": "B", "charlie": "C", "delta": "D",
    "echo": "E", "foxtrot": "F", "golf": "G", "hotel": "H",
    "india": "I", "juliet": "J", "kilo": "K", "lima": "L",
    "mike": "M", "november": "N", "oscar": "O", "papa": "P",
    "quebec": "Q", "romeo": "R", "sierra": "S", "tango": "T",
    "uniform": "U", "victor": "V", "whiskey": "W", "xray": "X",
    "x-ray": "X", "yankee": "Y", "zulu": "Z",
}

_nato_word = "(?:" + "|".join(re.escape(w) for w in _NATO) + ")"
_NATO_SEP   = r"[ \t]*,?[ \t]*"
_NATO_PATTERN = re.compile(rf"(?i)\b{_nato_word}(?:{_NATO_SEP}{_nato_word})*\b")
_SINGLE_LETTERS_PATTERN = re.compile(r'\b[A-Z](?:[ \t,]+[A-Z])+\b')


def _deduplicate_ticker(ticker: str) -> str:
    """Handle repeated ticker sequences. E.g., "ELPWELPW" → "ELPW"."""
    if len(ticker) < 4 or len(ticker) > 10:
        return ticker
    
    for divisor in range(2, min(6, len(ticker) // 2 + 1)):  # max 5 repetitions
        if len(ticker) % divisor == 0:
            segment_len = len(ticker) // divisor
            if not (2 <= segment_len <= 5):
                continue
                
            segment = ticker[:segment_len]
            if all(ticker[i*segment_len:(i+1)*segment_len] == segment 
                   for i in range(divisor)):
                return segment
    return ticker


def normalize_transcript(text: str) -> str:
    # 1. Collapse NATO alphabet sequences
    def collapse_nato(m: re.Match) -> str:
        words = re.split(r'[\s,]+', m.group(0).lower())
        letters = "".join(_NATO.get(w, "") for w in words if w)
        letters = _deduplicate_ticker(letters)
        return letters if 2 <= len(letters) <= 5 else m.group(0)

    text = _NATO_PATTERN.sub(collapse_nato, text)

    # 2. Collapse space/comma-separated single letters - apply repeatedly
    def collapse_single_letters(m: re.Match) -> str:
        letters = re.sub(r'[\s,]+', '', m.group(0).upper())
        letters = _deduplicate_ticker(letters)
        return letters if 2 <= len(letters) <= 5 else m.group(0)
    
    prev_text = None
    while prev_text != text:
        prev_text = text
        text = _SINGLE_LETTERS_PATTERN.sub(collapse_single_letters, text)

    # 3. Collapse dot-separated letters
    def collapse_dots(m: re.Match) -> str:
        letters = m.group(0).replace(".", "").upper()
        return letters if 2 <= len(letters) <= 5 else m.group(0)

    text = re.sub(r'(?<!\w)(?:[A-Za-z]\.)+(?:[A-Za-z])', collapse_dots, text)

    # 4. Collapse hyphen-separated letters
    def collapse_hyphens(m: re.Match) -> str:
        letters = m.group(0).replace("-", "").upper()
        return letters if 2 <= len(letters) <= 5 else m.group(0)

    text = re.sub(r'(?<!\w)(?:[A-Za-z]-)+[A-Za-z](?!\w)', collapse_hyphens, text)

    return text


# ========================= TESTS =========================

def test_normalize_transcript():
    """Test transcript normalization with various input formats."""
    
    test_cases = [
        ("E L P W", "ELPW"),
        ("E L P W E L P W", "ELPW"),  # Repetition (your key issue!)
        ("echo lima papa whiskey", "ELPW"),  # NATO phonetic
        ("echo lima papa whiskey echo lima papa whiskey", "ELPW"),  # NATO repeated
        ("E-L-P-W", "ELPW"),  # Hyphenated
        ("E.L.P.W", "ELPW"),  # Dotted
        ("E, L, P, W", "ELPW"),  # Comma-separated
        ("AAPL", "AAPL"),  # Already a ticker
        ("TSLA and NVDA", "TSLA"),  # Multiple tickers
    ]
    
    print("=" * 70)
    print("NORMALIZE_TRANSCRIPT TESTS")
    print("=" * 70)
    
    passed = 0
    failed = 0
    
    for input_text, expected in test_cases:
        result = normalize_transcript(input_text)
        success = expected.upper() in result.upper()
        status = "✓ PASS" if success else "✗ FAIL"
        passed += success
        failed += not success
        
        symbol = "→" if success else "✗"
        print(f"{status:8} {symbol:3} Input:  {input_text!r:<40}")
        print(f"           Output: {result!r}")
        if not success:
            print(f"           Expected to contain: {expected!r}")
        print()
    
    print(f"Normalize Results: {passed} passed, {failed} failed\n")
    return failed == 0


def test_deduplication():
    """Test the deduplication logic specifically."""
    
    test_cases = [
        ("ELPW", "ELPW"),  # No repeat
        ("ELPWELPW", "ELPW"),  # 2x repeat
        ("AAPLAAPL", "AAPL"),  # 2x repeat
        ("TSLATSLA", "TSLA"),  # 2x repeat (corrected from typo version)
        ("AABBCCDD", "AABBCCDD"),  # Not a repeating pattern
        ("XY", "XY"),  # Too short to deduplicate
        ("NVDANVDA", "NVDA"),  # Random non-repeating - check if normalized
    ]
    
    print("=" * 70)
    print("DEDUPLICATION TESTS")
    print("=" * 70)
    
    passed = 0
    failed = 0
    
    for input_ticker, expected in test_cases:
        result = _deduplicate_ticker(input_ticker)
        success = result == expected
        status = "✓ PASS" if success else "✗ FAIL"
        passed += success
        failed += not success
        
        print(f"{status:8} | Input: {input_ticker:<20} → {result:<15} (expected: {expected})")
    
    print(f"\nDeduplication Results: {passed} passed, {failed} failed\n")
    return failed == 0


def test_end_to_end():
    """Test the complete pipeline: normalize then verify."""
    
    test_cases = [
        ("buy E L P W at market", "ELPW"),  # Your original issue
        ("sell E L P W E L P W now", "ELPW"),  # Repeated
        ("echo lima papa whiskey echo lima papa whiskey", "ELPW"),  # NATO repeated
        ("buy AAPL sell TSLA", "AAPL"),  # Already uppercase
        ("NVDA, AAPL, TSLA breakout", "AAPL"),  # Multiple
        ("I like E-L-P-W", "ELPW"),  # Hyphenated
        ("E.L.P.W stock", "ELPW"),  # Dotted
    ]
    
    print("=" * 70)
    print("END-TO-END TESTS (normalize)")
    print("=" * 70)
    
    passed = 0
    failed = 0
    
    for raw, expected_ticker in test_cases:
        normalized = normalize_transcript(raw)
        success = expected_ticker in normalized
        status = "✓ PASS" if success else "✗ FAIL"
        passed += success
        failed += not success
        
        print(f"{status:8} | Raw:        {raw!r:<40}")
        print(f"           Normalized: {normalized!r}")
        if not success:
            print(f"           Expected to contain: {expected_ticker!r}")
        print()
    
    print(f"End-to-End Results: {passed} passed, {failed} failed\n")
    return failed == 0


if __name__ == "__main__":
    import sys
    
    all_pass = True
    all_pass &= test_normalize_transcript()
    all_pass &= test_deduplication()
    all_pass &= test_end_to_end()
    
    print("=" * 70)
    if all_pass:
        print("✅ All tests passed!")
        sys.exit(0)
    else:
        print("❌ Some tests failed")
        sys.exit(1)
