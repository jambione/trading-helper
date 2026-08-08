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
SECRETS_FILE = ROOT / "config" / "secrets.json"

# secrets.json key -> the environment variable the desk reads it as.
#
# Credentials have two audiences: six modules read cfg["api_key"] (which
# load_config fills from secrets.json), and fourteen read os.getenv(
# "ALPACA_API_KEY") (which came from signal_engine.env). Two stores, no link
# between them — they happened to agree, but nothing made them.
#
# secrets.json is the source of truth. load_desk_env() overlays it onto the
# environment so the env-reading half sees the same values without each of
# those fourteen modules having to change how it reads.
CREDENTIAL_ENV = {
    "api_key": "ALPACA_API_KEY",
    "secret_key": "ALPACA_SECRET_KEY",
    "finnhub_key": "FINNHUB_API_KEY",
}


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


def apply_secrets_to_env(
    secrets_path: Path | str | None = None,
    *,
    overridable: frozenset[str] | set[str] = frozenset(),
) -> list[str]:
    """Overlay credentials from secrets.json onto ``os.environ``.

    *overridable* is the set of variables that came from an env FILE rather
    than the real shell — normally ``load_env_file()``'s return value. Those
    may be replaced, because secrets.json outranks signal_engine.env. Anything
    else already in the environment is a genuine shell value and is left alone,
    which keeps ``ALPACA_API_KEY=... ./trading start`` working as an override.

    Resulting precedence:  shell  >  secrets.json  >  signal_engine.env

    Returns the variables it set. Never raises and never logs a value — a
    missing or malformed secrets.json just means no overlay, and the caller
    falls back to whatever the env file provided.
    """
    p = Path(secrets_path) if secrets_path is not None else SECRETS_FILE
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []

    applied: list[str] = []
    for cfg_key, env_key in CREDENTIAL_ENV.items():
        value = data.get(cfg_key)
        if not isinstance(value, str) or not value.strip():
            continue
        if env_key in os.environ and env_key not in overridable:
            continue  # a real shell value — do not clobber it
        os.environ[env_key] = value.strip()
        applied.append(env_key)
    return applied


def load_desk_env(
    env_path: Path | str | None = None,
    secrets_path: Path | str | None = None,
) -> list[str]:
    """Load signal_engine.env, then let secrets.json win on credentials.

    This is what desk processes should call at startup. Returns every variable
    that came from a file, so callers tracking keys for an ``os.execv`` restart
    (signal_engine, ai_trader) drop the overlaid credentials too and re-read
    them on the way back up.
    """
    from_file = load_env_file(env_path)
    applied = apply_secrets_to_env(secrets_path, overridable=set(from_file))
    return from_file + [k for k in applied if k not in from_file]


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
