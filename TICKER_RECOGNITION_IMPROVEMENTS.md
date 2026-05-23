# Ticker Recognition Improvements

## Current System Overview

The transcription system in `transcription/transcribe_action.py` uses three strategies:

1. **NATO Alphabet Conversion** - Converts NATO phonetic words to letters (e.g., "echo lima papa whiskey" → "ELPW")
2. **Stop Word Filtering** - Excludes common English words
3. **Ticker Universe Validation** - Validates against a cached list of valid US equity symbols from Alpaca

## Problem: Why "E L P W E L P W E L P W" Isn't Recognized

Your example highlights several issues:

### Issue 1: Repetition in Transcripts

The current NATO pattern:

```regex
(?i)\b(echo|alpha|bravo|...)([ \t]*,?[ \t]*)*(echo|alpha|bravo|...){1,4}\b
```

Only matches **1-4 consecutive NATO words**. When the ASR (speech recognition) repeats the sequence for clarity, it's not captured.

### Issue 2: Mixed Formats

The system doesn't handle when already-spoken single letters appear:

- `E L P W` (letters) should match
- `echo lima papa whiskey` (NATO) should match
- `E echo L lima P papa W whiskey` (mixed) is not handled

### Issue 3: No Fuzzy Matching

No tolerance for mishearings:

- `EL PW` (spacing variation)
- `L.P.W` (punctuation)
- `ELPP` → `ELPW` (OCR/ASR errors)

## Recommended Improvements

### 1. **Enhance NATO Pattern to Handle Repetitions**

Replace the current pattern with one that collapses repeated sequences:

```python
# Instead of matching 1-4 words, keep matching and deduplicate
def collapse_repeated_nato(m: re.Match) -> str:
    """Collapse repeated NATO sequences into single letters"""
    words = re.split(r'[\s,]+', m.group(0).lower())
    letters_seq = [_NATO.get(w, "") for w in words if w]

    # For repeated sequences like "E L P W E L P W", detect and collapse
    ticker = "".join(letters_seq)

    # Try to detect repeating pattern (ELPWELPW → ELPW)
    if len(ticker) >= 4 and len(ticker) % 2 == 0:
        half_len = len(ticker) // 2
        if ticker[:half_len] == ticker[half_len:]:
            ticker = ticker[:half_len]

    return ticker if 2 <= len(ticker) <= 5 else m.group(0)
```

### 2. **Add Support for Individual Letters**

Add detection for already-spoken single letters:

```python
# Pattern to match sequences of single capital letters separated by spaces/commas
_SINGLE_LETTERS_PATTERN = re.compile(r'\b([A-Z])(?:[ \t]*,?[ \t]*(?=[A-Z]))*\b')

def collapse_single_letters(m: re.Match) -> str:
    """Collapse space-separated letters into ticker"""
    # Find all letters in this sequence
    letters = re.findall(r'[A-Z]', m.group(0))
    ticker = "".join(letters)
    return ticker if 2 <= len(ticker) <= 5 else m.group(0)
```

### 3. **Add Fuzzy Matching for Close Calls**

Use edit distance to catch likely typos:

```python
from difflib import SequenceMatcher

def find_close_ticker(candidate: str, universe: set, threshold: float = 0.85) -> Optional[str]:
    """
    If candidate isn't in universe, find close match.
    E.g., "ELPP" → "ELPW" (2 chars different in 4-char word = 50% mismatch, too high)
    But "ELPL" → "ELPW" (1 char different = 75% match, might accept)
    """
    if candidate in universe:
        return candidate

    for symbol in universe:
        if len(symbol) == len(candidate):
            ratio = SequenceMatcher(None, candidate, symbol).ratio()
            if ratio >= threshold:
                return symbol
    return None
```

### 4. **Enhance Ticker Universe with Priority List**

Add a "hot list" of commonly traded tickers to improve Whisper priming:

```python
# Add to bot_config.json or as a constant
HOT_TICKERS = [
    # Tech giants
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "TSLA", "META",
    # Finance/Brokers
    "JPM", "GS", "BAC", "WFC", "HOOD", "COIN", "SOFI",
    # Your personal watchlist
    "ELPW",  # Add problem tickers here
    # ... etc
]

# Prepend hot tickers to prompt for better ASR attention
INITIAL_PROMPT = _PROMPT_BASE + f" Trading focus: {' '.join(HOT_TICKERS)}."
```

### 5. **Add Logging for Debugging**

Track what transcripts produce what ticker extractions:

```python
def extract_tickers_debug(text: str, debug: bool = False) -> tuple:
    """Returns (found_tickers, debug_info)"""
    debug_info = {
        "raw_text": text,
        "normalized": normalize_transcript(text),
        "nato_matches": [],
        "letter_matches": [],
        "universe_filtered": [],
    }
    # ... extraction logic with tracking ...
    return found, debug_info
```

## Implementation Priority

1. **HIGH**: Fix NATO pattern to handle repetitions (Issue #1) - addresses your "ELPW" example directly
2. **HIGH**: Add single-letter detection - many tickers are spelled out as letters
3. **MEDIUM**: Add fuzzy matching - catches transcription errors
4. **MEDIUM**: Add hot-list priming - improves ASR accuracy upfront
5. **LOW**: Add debug logging - helps identify other edge cases

## Testing Strategy

Create test cases for known problem transcripts:

```python
test_cases = [
    ("E L P W", ["ELPW"]),                                          # Issue case
    ("E L P W E L P W", ["ELPW"]),                                 # Repeated
    ("echo lima papa whiskey", ["ELPW"]),                          # NATO
    ("E-L-P-W", ["ELPW"]),                                         # Hyphenated
    ("ELPW", ["ELPW"]),                                            # Already recognized
    ("apple", ["AAPL"]),                                           # Company name
    ("E L P W, sell at fifty", ["ELPW"]),                         # With command
    ("echo lima papa whiskey echo lima papa whiskey", ["ELPW"]),  # Spoken twice
]
```

## Files to Modify

1. **`transcription/transcribe_action.py`** - Update `normalize_transcript()` and `extract_tickers()`
2. **`bot_config.json`** - Add `hot_tickers` config option
3. **Create `transcription/ticker_recognizer_test.py`** - Add unit tests

## Additional Notes

- The ticker universe loads from Alpaca at startup, but you can manually add tickers to `transcription/ticker_universe.json`
- Check if "ELPW" is in the universe file - if not, it will be rejected even if normalized correctly
- Consider running with `--device` flag to select better audio input device for clearer recognition
