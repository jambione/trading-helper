#!/usr/bin/env python3
"""
Test ticker recognition improvements.

Run with:
  cd transcription
  python test_ticker_recognition.py
"""

import sys
from pathlib import Path

# Add parent dir to path so we can import transcribe_action
sys.path.insert(0, str(Path(__file__).parent))

from transcribe_action import normalize_transcript, extract_tickers


def test_normalize_transcript():
    """Test transcript normalization with various input formats."""
    
    test_cases = [
        # (input, expected_contains)
        ("E L P W", "ELPW"),
        ("E L P W E L P W", "ELPW"),  # Repetition
        ("E L P W E L P W E L P W", "ELPW"),  # Triple repetition
        ("echo lima papa whiskey", "ELPW"),  # NATO phonetic
        ("echo lima papa whiskey echo lima papa whiskey", "ELPW"),  # NATO repeated
        ("E-L-P-W", "ELPW"),  # Hyphenated
        ("E.L.P.W", "ELPW"),  # Dotted
        ("E, L, P, W", "ELPW"),  # Comma-separated
        ("AAPL", "AAPL"),  # Already a ticker
        ("apple", "apple"),  # Company name (handled by extract_tickers, not normalize)
    ]
    
    print("=" * 60)
    print("NORMALIZE_TRANSCRIPT TESTS")
    print("=" * 60)
    
    passed = 0
    failed = 0
    
    for input_text, expected in test_cases:
        result = normalize_transcript(input_text)
        success = expected.upper() in result.upper()
        status = "✓ PASS" if success else "✗ FAIL"
        passed += success
        failed += not success
        
        print(f"{status} | Input: {input_text!r:<45} → {result!r}")
        if not success:
            print(f"       Expected to contain: {expected!r}")
    
    print(f"\nNormalize Results: {passed} passed, {failed} failed")
    return failed == 0


def test_extract_tickers():
    """Test ticker extraction from normalized transcripts."""
    
    test_cases = [
        # (normalized_text, expected_tickers_or_partial_match)
        ("I will buy AAPL today", ["AAPL"]),
        ("Sell ELPW at 50", ["ELPW"]),  # Your example case
        ("ELPW repeated ELPW", ["ELPW"]),  # Should dedup
        ("TSLA and NVDA breakout", ["TSLA", "NVDA"]),
        ("sell the DIS and hold QQQ", ["DIS", "QQQ"]),
        ("I want to buy apple stock", ["AAPL"]),  # Company name
        ("Facebook and Netflix", ["META", "NFLX"]),  # Multiple company names
        ("This IS NOT a ticker", []),  # Stop words filtered
    ]
    
    print("\n" + "=" * 60)
    print("EXTRACT_TICKERS TESTS")
    print("=" * 60)
    
    passed = 0
    failed = 0
    
    for text, expected in test_cases:
        result = extract_tickers(text)
        
        # Check if we got all expected tickers
        success = all(t in result for t in expected) and len(result) == len(expected)
        status = "✓ PASS" if success else "✗ FAIL"
        passed += success
        failed += not success
        
        print(f"{status} | Text: {text!r:<40}")
        print(f"       Expected: {expected}")
        print(f"       Got:      {result}")
    
    print(f"\nExtract Results: {passed} passed, {failed} failed")
    return failed == 0


def test_end_to_end():
    """Test the complete pipeline: normalize then extract."""
    
    test_cases = [
        # (raw_transcript, expected_tickers)
        ("buy E L P W at market", ["ELPW"]),
        ("sell E L P W E L P W now", ["ELPW"]),
        ("echo lima papa whiskey echo lima papa whiskey", ["ELPW"]),
        ("buy AAPL sell TSLA", ["AAPL", "TSLA"]),
        ("I like apple and tesla", ["AAPL", "TSLA"]),
    ]
    
    print("\n" + "=" * 60)
    print("END-TO-END TESTS (normalize → extract)")
    print("=" * 60)
    
    passed = 0
    failed = 0
    
    for raw, expected in test_cases:
        normalized = normalize_transcript(raw)
        result = extract_tickers(normalized)
        
        success = all(t in result for t in expected) and len(result) == len(expected)
        status = "✓ PASS" if success else "✗ FAIL"
        passed += success
        failed += not success
        
        print(f"{status} | Raw: {raw!r}")
        print(f"       Normalized: {normalized!r}")
        print(f"       Expected: {expected}")
        print(f"       Got:      {result}")
    
    print(f"\nEnd-to-End Results: {passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    all_pass = True
    
    all_pass &= test_normalize_transcript()
    all_pass &= test_extract_tickers()
    all_pass &= test_end_to_end()
    
    print("\n" + "=" * 60)
    if all_pass:
        print("✓ All tests passed!")
        sys.exit(0)
    else:
        print("✗ Some tests failed")
        sys.exit(1)
