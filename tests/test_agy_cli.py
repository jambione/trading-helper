"""Antigravity CLI (agy -p) as the Anthropic research-slot backend."""
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import ai_suggest as m  # noqa: E402


def test_agy_is_the_anthropic_slot():
    assert m.is_agy_backend("agy")
    assert m.is_agy_backend("gemini_cli")
    assert m.is_agy_backend("claude_cli", cli_bin="agy")
    assert not m.is_agy_backend("claude_cli", cli_bin="claude")
    assert m.source_from_backend("agy") == "anthropic"
    assert m.normalize_ai_source("gemini") == "anthropic"


def test_agy_model_and_effort_maps():
    assert m._agy_model("sonnet") == m.DEFAULT_AGY_MODEL
    assert m._agy_model("grok-4.5") == m.DEFAULT_AGY_MODEL
    assert m._agy_model("gemini-3.1-pro-high") == "gemini-3.1-pro-high"
    assert m._agy_effort("xhigh") == "high"
    assert m._agy_effort("low") == "low"
    assert m._agy_print_timeout(600) == "600s"


def test_call_agy_cli_parses_the_probe_envelope(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "resolve_agy_cli", lambda bin=None: "/fake/agy")
    monkeypatch.setattr(m, "_cli_workspace", lambda: str(tmp_path))
    captured = {}

    class _P:
        returncode = 0
        stdout = json.dumps({
            "conversation_id": "e2a5c053-81dd-4de2-aeed-0022997d8dac",
            "status": "SUCCESS",
            "response": "PONG\n",
            "duration_seconds": 2.25,
            "num_turns": 1,
            "usage": {
                "input_tokens": 13732,
                "output_tokens": 27,
                "thinking_tokens": 25,
                "cache_read_tokens": 0,
                "total_tokens": 13759,
            },
        })
        stderr = ""

    def _run(cmd, **kw):
        captured["cmd"] = cmd
        captured["kw"] = kw
        return _P()

    monkeypatch.setattr(m.subprocess, "run", _run)
    text = m.call_agy_cli("Reply PONG", timeout=90, effort="xhigh")
    assert text.strip() == "PONG"
    cmd = captured["cmd"]
    assert cmd[0] == "/fake/agy"
    assert "-p" in cmd
    assert "--output-format" in cmd and "json" in cmd
    assert "--dangerously-skip-permissions" not in cmd
    assert "--model" in cmd and m.DEFAULT_AGY_MODEL in cmd
    assert "--effort" in cmd and "high" in cmd
    assert captured["kw"].get("cwd") == str(tmp_path)


def test_call_agy_cli_raises_on_auth_error(monkeypatch):
    monkeypatch.setattr(m, "resolve_agy_cli", lambda bin=None: "/fake/agy")

    class _P:
        returncode = 1
        stdout = json.dumps({
            "conversation_id": "",
            "status": "ERROR",
            "response": "",
            "error": "authentication failed or timed out",
        })
        stderr = "Error: authentication required. Run 'agy' to log in, then retry.\n"

    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: _P())
    with pytest.raises(RuntimeError, match="not logged in"):
        m.call_agy_cli("hi", timeout=30)


def test_agy_auth_status_uses_models(monkeypatch):
    monkeypatch.setattr(m, "resolve_agy_cli", lambda bin=None: "/fake/agy")

    class _P:
        returncode = 0
        stdout = "gemini-3.7-flash-high\tGemini 3.7 Flash (High)\n"
        stderr = "Fetching available models...\n"

    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: _P())
    st = m.agy_auth_status()
    assert st["logged_in"] is True

    class _Fail:
        returncode = 1
        stdout = ""
        stderr = "Error: Please sign in to view available models."

    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: _Fail())
    st2 = m.agy_auth_status()
    assert st2["logged_in"] is False
