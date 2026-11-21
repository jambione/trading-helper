"""Open a text log that may have been compressed by scripts/compress_old_logs.sh (FILE or FILE.gz). Pure helpers."""
from __future__ import annotations

import gzip
import os


def resolve(path) -> str | None:
    """The existing file for `path`: itself, else `path`.gz, else None."""
    p = str(path)
    if os.path.exists(p):
        return p
    if os.path.exists(p + ".gz"):
        return p + ".gz"
    return None


def open_text(path, errors: str = "replace"):
    """Text-mode handle for FILE or FILE.gz (raises FileNotFoundError if neither exists)."""
    p = resolve(path)
    if p is None:
        raise FileNotFoundError(str(path))
    return gzip.open(p, "rt", encoding="utf-8", errors=errors) if p.endswith(".gz") else open(p, encoding="utf-8", errors=errors)
