# CODX Ticker Recognition Fix — Root Cause Analysis

## Problem
CODX ticker was not being recognized in transcribed audio despite being in the valid ticker list and not in stop words.

## Root Cause
The TTS system (Text-To-Speech) used by CNBC pronounces the letter X as **"eks"** or **"ecks"**, not **"ex"**.

When Whisper transcribed spoken CODX as "see oh dee eks":
- **Before fix:** `"see oh dee eks"` → `"COD eks"` ❌ (only matched "COD", missing X)
- **After fix:** `"see oh dee eks"` → `"CODX"` ✓

## Why It Happened
The `_LETTER_NAMES` dictionary in `transcribe_action.py` only included the letter name pronunciations that were literally spelled out in the code:
- Had: `"ex": "X"`
- Missing: `"eks": "X"` and `"ecks": "X"`

This caused the letter-name normalization regex to fail to match "eks" as a letter name.

## Solution
Added both common TTS pronunciations for the letter X to `_LETTER_NAMES`:

```python
_LETTER_NAMES = {
    # ... other letters ...
    "vee":     "V", "ex":     "X", "eks":    "X", "ecks":  "X", "wye":   "Y", "why":   "Y",
    # ... other letters ...
}
```

## Testing
Created diagnostic tools to identify the issue:

1. **diagnose_codx.py** — Checks if CODX is in valid tickers and tests phonetic matching
   - Verified CODX is in valid_tickers.txt ✓
   - Verified CODX is not in stop_words ✓
   - Confirmed single-char confusion pairs can catch mishears ✓

2. **profile_codx_pipeline.py** — Tests normalization pipeline against potential Whisper outputs
   - Identified "see oh dee eks" → "COD eks" (failure)
   - Identified "see oh dee ex" → "CODX" (success)
   - Root cause: missing "eks" pronunciation

## Impact
- **Direct fix:** CODX and any ticker with X (APEX, AXIOM, AXON, etc.) are now recognized when spoken with "eks" pronunciation
- **Systemic improvement:** More accurate phonetic normalization for all letter-spelled tickers
- **Future-proofing:** Confirms the normalization pipeline works correctly for other edge cases

## Related Files Modified
- `transcription/transcribe_action.py` — Added "eks" and "ecks" to `_LETTER_NAMES` dict (line 126)

## Validation
Test the fix with:
```bash
python3 transcription/diagnose_codx.py
python3 transcription/profile_codx_pipeline.py
```

Both diagnostics now show improved CODX recognition.
