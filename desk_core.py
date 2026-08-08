"""desk_core.py — primitives shared across the desk.

Each helper here existed as a copy-pasted private function in anywhere from 4 to
14 modules. They were byte-identical often enough that a fix to one silently
failed to reach the others, so they live here now and the modules import them.

Root-level on purpose: ``momentum-monitor/`` and ``tv-monitor/`` have hyphens in
their names and are not importable packages, so they reach shared code through
the established ``sys.path.insert(0, str(ROOT))`` pattern. A root module is the
only thing every caller can see.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / "signal_engine.env"


def load_env_file(path: Path | str | None = None) -> list[str]:
    """Parse KEY=VALUE lines from an env file into ``os.environ``.

    The shell environment always wins — only keys that aren't already set are
    injected. Returns the list of keys actually loaded, which the engine and
    the AI trader need: on a dashboard-triggered restart they pop exactly these
    before ``os.execv`` so the re-exec re-reads an edited file rather than
    inheriting the stale values.

    Values are split on ``" #"`` rather than a bare ``"#"``. That matters —
    secrets can legitimately contain a hash, and the secret-allowlist pragmas
    in signal_engine.env live on the value line. Surrounding quotes are
    stripped, which is ordinary env-file idiom.
    """
    p = Path(path) if path is not None else ENV_FILE
    if not p.exists():
        return []
    loaded: list[str] = []
    with open(p, encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.split(" #", 1)[0].strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
                loaded.append(key)
    return loaded


def parse_hhmm(raw: Any, default: tuple[int, int]) -> tuple[int, int]:
    """``"15:50"`` -> ``(15, 50)``. Returns *default* on anything unparseable."""
    try:
        hh, mm = str(raw or "").strip().split(":")
        return int(hh), int(mm)
    except Exception:
        return default


def write_json_atomic(path: Path, payload: Any, *, indent: int = 2) -> None:
    """Write JSON via a temp file + rename, so readers never see a partial file.

    The dashboard reads several of these files on a poll while the engine is
    writing them, which is what the atomic replace is protecting against.
    Raises on failure — callers that must not crash wrap this themselves.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=indent, default=str)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        Path(tmp_path).replace(path)
        tmp_path = None   # rename succeeded — nothing to clean up
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def to_float(v: Any) -> float | None:
    """``float(v)`` or None — never raises on broker/model payload junk."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def to_int(v: Any) -> int | None:
    """``int(v)`` or None. Goes through float first so ``"3.0"`` parses."""
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None
