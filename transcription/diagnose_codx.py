#!/usr/bin/env python3
"""Diagnose why CODX ticker is not being recognized."""

import sys

# Load validation logic from transcribe_action
_VALID_TICKERS_FILE = "valid_tickers.txt"
try:
    _VALID_TICKERS = set(open(_VALID_TICKERS_FILE).read().split())
except Exception as e:
    print(f"ERROR: Could not load {_VALID_TICKERS_FILE}: {e}")
    sys.exit(1)

print(f"Loaded {len(_VALID_TICKERS):,} valid tickers\n")

# Check if CODX is in the list
if "CODX" in _VALID_TICKERS:
    print("✓ CODX is in valid_tickers.txt")
else:
    print("✗ CODX is NOT in valid_tickers.txt")
    sys.exit(1)

# Stop words check
_STOP_WORDS = {
    "A", "I", "AN", "AS", "AT", "BE", "BY", "DO", "GO", "IF", "IN", "IS",
    "IT", "ME", "MY", "NO", "OF", "OK", "ON", "OR", "SO", "TO", "UP", "US",
    "WE", "AND", "ARE", "BUT", "CAN", "DID", "FOR", "GET", "GOT", "HAD",
    "HAS", "HIM", "HIS", "HOW", "HER", "ITS", "NEW", "NOT", "NOW", "OLD",
    "ONE", "OUR", "OUT", "SAY", "SEE", "THE", "TOO", "TWO", "WAS", "WHO",
    "WHY", "YET", "YOU", "ALL", "ANY", "LET", "PUT", "RUN", "SET", "ADD",
    "BUY", "HIT", "TRY", "USE", "WAY", "DAY", "MAY", "OWN", "ASK", "ACT",
    "ALSO", "BACK", "BEEN", "CALL", "COME", "DOES", "DOWN", "EACH", "EVEN",
    "FROM", "GIVE", "GOOD", "HAVE", "HERE", "HIGH", "HOLD", "INTO", "JUST",
    "KEEP", "KNOW", "LAST", "LIKE", "LIVE", "LONG", "LOOK", "MADE", "MAKE",
    "MANY", "MORE", "MOST", "MOVE", "MUCH", "MUST", "NEXT", "ONLY",
    "OVER", "PAST", "SAME", "SELL", "SHOW", "SIDE", "SOME",
    "STOP", "SUCH", "TAKE", "THAN", "THAT", "THEM", "THEN", "THEY", "THIS",
    "TIME", "VERY", "WANT", "WELL", "WHAT", "WHEN", "WILL", "WITH", "WORK",
    "YOUR", "SAID", "SAYS", "TOLD", "TELL", "TALK", "WENT", "GOES", "BOTH",
    "ONCE", "UPON", "SOON", "EVER", "YEAR", "WEEK", "DAYS", "LETS", "PUTS",
    "ALSO", "THEN", "THAN", "BEEN", "WERE", "HAVE", "MAKE",
    "ZERO", "ONE", "TWO", "THREE", "FOUR", "FIVE", "SIX", "SEVEN", "EIGHT", "NINE",
    "TEN", "ELEVEN", "TWELVE", "THIRTEEN", "TWENTY", "THIRTY", "FORTY", "FIFTY",
    "HUNDRED", "THOUSAND", "MILLION", "BILLION",
    "ETF", "IPO", "CEO", "CFO", "COO", "CTO", "SEC", "FDA", "FED", "GDP",
    "CPI", "EPS", "ATH", "ATL", "RSI", "SMA", "EMA", "BEAR", "BULL",
    "CALL", "PUTS", "SPAC", "REIT", "BOND", "DEBT", "CASH", "RATE", "RISK",
    "LOSS", "GAIN", "NEWS", "CNBC", "NYSE", "VWAP", "MACD",
    "HALT", "ALERT", "LEVEL", "STOCK", "PRICE", "TRADE", "SHARE",
    "OPEN", "HIGH", "CLOSE", "AFTER", "ABOVE", "BELOW", "RANGE",
    "BEAT", "MISS", "GUIDE", "VIEW", "RAISE", "LOWER", "CUTS",
    "SPIKE", "SPIKE", "VOLUME", "VOLATILITY", "FLAG", "FACILITY",
    "MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN",
    "JAN", "FEB", "MAR", "APR", "AUG", "SEP", "OCT", "NOV", "DEC",
    "HEY", "YEAH", "OKAY", "WELL", "LIKE", "JUST", "SAID",
    "STILL", "REALLY", "PRETTY", "MIGHT", "MAYBE",
}

if "CODX" in _STOP_WORDS:
    print("✗ CODX is in stop words (will be filtered)")
else:
    print("✓ CODX is NOT in stop words")

# Phonetic confusions
_PHONETIC_CONFUSIONS = {
    'A': ['E', 'I'], 'E': ['A', 'I'], 'I': ['A', 'E', 'Y'],
    'O': ['U', 'A'], 'U': ['O', 'A'],
    'B': ['V', 'D', 'P', 'G'], 'V': ['B', 'F'], 'P': ['B', 'F'],
    'D': ['T', 'B', 'G'], 'T': ['D'],
    'M': ['N', 'B'], 'N': ['M', 'D'],
    'S': ['Z', 'C', 'X'], 'Z': ['S', 'X'], 'C': ['S', 'K'],
    'G': ['C', 'D', 'K'], 'K': ['C', 'G'], 'X': ['S', 'Z'],
    'F': ['V', 'P'], 'Y': ['I', 'J'],
}

def _levenshtein_distance(s1: str, s2: str) -> int:
    """Compute Levenshtein distance between two strings."""
    if len(s1) < len(s2):
        return _levenshtein_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)
    prev_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        cur_row = [i + 1] + [0] * len(s2)
        for j, c2 in enumerate(s2):
            cur_row[j + 1] = min(
                prev_row[j + 1] + 1,
                cur_row[j] + 1,
                prev_row[j] + (0 if c1 == c2 else 1),
            )
        prev_row = cur_row
    return prev_row[-1]

# Test common CODX mishears
print("\n" + "="*70)
print("TESTING COMMON CODX MISHEARS")
print("="*70)

# CODX is C-O-D-X. Common mishears from TTS:
test_candidates = [
    "CODX",     # correct
    "COTX",     # D→T
    "KODX",     # C→K
    "CODZ",     # X→Z
    "CODKS",    # misheard as C-O-D-K-S
    "CODG",     # X→G
    "COWX",     # D→W (unlikely)
    "CODS",     # missing X
    "CODE",     # X→E
    "CODA",     # X→A
    "CODD",     # X→D (repeated)
    "CODBX",    # inserted B
    "COXD",     # reordered
    "CODEX",    # added E (like "Codex")
]

for candidate in test_candidates:
    if len(candidate) > 5:
        print(f"{candidate:10} → SKIP (len={len(candidate)} > 5)")
        continue

    print(f"\n{candidate:10} (len={len(candidate)})")

    # Check exact match
    if candidate in _VALID_TICKERS:
        print("  ✓ Exact match in valid tickers")
        continue

    # Check single-char substitutions
    found = False
    for i, ch in enumerate(candidate):
        for swap in _PHONETIC_CONFUSIONS.get(ch, []):
            fixed = candidate[:i] + swap + candidate[i+1:]
            if fixed in _VALID_TICKERS:
                print(f"  ✓ Single-char swap: {ch}→{swap} → {fixed}")
                found = True

    # Check Levenshtein distance 1
    if not found:
        for ticker in _VALID_TICKERS:
            if len(ticker) == len(candidate) and _levenshtein_distance(candidate, ticker) == 1:
                print(f"  ✓ Levenshtein dist=1 → {ticker}")
                found = True

    if not found:
        print("  ✗ No match found")

print("\n" + "="*70)
print("ANALYZING CODX PATTERN")
print("="*70)

# CODX = C O D X. What are the character confusions?
print("\nCODX character confusions:")
for i, ch in enumerate("CODX"):
    confusions = _PHONETIC_CONFUSIONS.get(ch, [])
    print(f"  {i}: {ch} can be confused with: {', '.join(confusions) if confusions else 'none'}")

# Find all tickers at distance 1 from CODX
print("\nTickers at Levenshtein distance 1 from CODX:")
dist1_tickers = []
for ticker in _VALID_TICKERS:
    if len(ticker) == 4 and _levenshtein_distance("CODX", ticker) == 1:
        dist1_tickers.append(ticker)

if dist1_tickers:
    for ticker in sorted(dist1_tickers):
        print(f"  {ticker}")
else:
    print("  (none found)")

# Find all tickers with same starting letters
print("\nTickers starting with CO:")
co_tickers = [t for t in _VALID_TICKERS if t.startswith("CO")]
print(f"  Found {len(co_tickers)} tickers: {', '.join(sorted(co_tickers)[:20])}")
if len(co_tickers) > 20:
    print(f"  ... and {len(co_tickers) - 20} more")

print("\n✓ Diagnosis complete")
