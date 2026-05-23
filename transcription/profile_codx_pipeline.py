#!/usr/bin/env python3
"""Profile CODX ticker transcription pipeline.

Run this alongside transcribe_action.py with logging enabled to see:
1. Raw Whisper output
2. Normalized transcript
3. Extracted ticker candidates
4. Final validation results
"""

import re

# Import the normalization logic
_NATO = {
    "alpha": "A", "bravo": "B", "charlie": "C", "delta": "D",
    "echo": "E", "foxtrot": "F", "golf": "G", "hotel": "H",
    "india": "I", "juliet": "J", "kilo": "K", "lima": "L",
    "mike": "M", "november": "N", "oscar": "O", "papa": "P",
    "quebec": "Q", "romeo": "R", "sierra": "S", "tango": "T",
    "uniform": "U", "victor": "V", "whiskey": "W", "xray": "X",
    "x-ray": "X", "yankee": "Y", "zulu": "Z",
}

_LETTER_NAMES = {
    "ay": "A", "bee": "B", "cee": "C", "see": "C",
    "dee": "D", "ee": "E", "ef": "F", "eff": "F",
    "gee": "G", "jee": "G", "aitch": "H", "haitch": "H",
    "eye": "I", "jay": "J", "kay": "K", "el": "L",
    "em": "M", "en": "N", "oh": "O", "pee": "P",
    "cue": "Q", "queue": "Q", "ar": "R", "arr": "R",
    "ess": "S", "es": "S", "tee": "T", "you": "U",
    "vee": "V", "ex": "X", "wye": "Y", "why": "Y",
    "zee": "Z", "zed": "Z",
}

_nato_word = "(?:" + "|".join(re.escape(w) for w in _NATO) + ")"
_NATO_SEP = r"[ \t]*,?[ \t]*"
_NATO_PATTERN = re.compile(rf"(?i)\b{_nato_word}(?:{_NATO_SEP}{_nato_word}){{1,4}}\b")

_letter_name_word = "(?:" + "|".join(re.escape(w) for w in _LETTER_NAMES) + ")"
_LETTER_NAME_SEP = r"[ \t]*[.\-,]?[ \t]*"
_LETTER_NAME_PATTERN = re.compile(
    rf"(?i)\b{_letter_name_word}(?:{_LETTER_NAME_SEP}{_letter_name_word}){{1,4}}\b"
)

def normalize_transcript(text: str) -> str:
    """Same normalization as transcribe_action.py"""
    def collapse_letter_names(m):
        words = re.split(r'[\s]+', m.group(0).lower())
        letters = "".join(_LETTER_NAMES.get(w, "") for w in words if w)
        return letters if 2 <= len(letters) <= 5 else m.group(0)

    text = _LETTER_NAME_PATTERN.sub(collapse_letter_names, text)

    def collapse_nato(m):
        words = re.split(r'[\s,]+', m.group(0).lower())
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

# Test transcripts that might be returned by Whisper for CODX
test_transcripts = [
    # Expected forms
    ("see oh dee eks", "Letter names (English)"),
    ("C O D X", "Spaced letters"),
    ("C.O.D.X", "Dot notation"),
    ("C-O-D-X", "Hyphen notation"),
    ("Charlie Oscar Delta X-ray", "NATO phonetics"),

    # Common Whisper mishears for CODX
    ("see oh dee ex", "Mishear: ex instead of eks"),
    ("see oh dee x", "Mishear: x instead of eks"),
    ("code x", "Mishear: code as word + x"),
    ("codex", "Mishear: codex (actual word)"),
    ("see oh d x", "Mishear: missing e in dee"),
    ("see oh dee ecks", "Variant: ecks instead of eks"),
    ("c o d x", "Lowercase spaced letters"),
    ("see o dee ex", "Mishear: o instead of oh"),
    ("coda x", "Mishear: coda (word) + x"),
    ("see oh t x", "Mishear: t instead of d"),
    ("see oh k x", "Mishear: k instead of d"),
    ("see oh d s", "Mishear: s instead of x"),
    ("see oh d z", "Mishear: z instead of x"),
]

print("="*80)
print("CODX TRANSCRIPTION PIPELINE PROFILING")
print("="*80)

for raw_transcript, description in test_transcripts:
    normalized = normalize_transcript(raw_transcript)

    # Extract 2-5 letter candidates
    candidates = re.findall(r'\b([A-Za-z]{2,5})\b', normalized)
    unique_candidates = list(dict.fromkeys(candidates))  # preserve order, remove dupes

    print(f"\nInput:      {raw_transcript!r}")
    print(f"            ({description})")
    print(f"Normalized: {normalized!r}")
    print(f"Candidates: {unique_candidates}")

    # Check for CODX or similar
    if "CODX" in unique_candidates:
        print("✓ CODX found!")
    elif any(c in unique_candidates for c in ["CODA", "CODE", "CODI"]):
        print(f"~ Found similar: {[c for c in unique_candidates if c in ['CODA', 'CODE', 'CODI']]}")
    else:
        print("✗ CODX not found in candidates")

print("\n" + "="*80)
print("KEY FINDINGS:")
print("="*80)
print("""
1. Letter-name normalization (see oh dee eks → CODX) requires exact matching
2. If Whisper mishears any vowel ("ex" → "x", "oh" → "o"), normalization fails
3. Common failures:
   - "see oh d x" → doesn't normalize (missing second vowel in "dee")
   - "code x" → extracts "CODE" and "X" separately (word collision)
   - "codex" → extracts "CODEX" (5 letters, still valid but different)
4. Solution: Ollama phonetic validation should catch these

Next: Check Ollama logs for CODX handling
""")
