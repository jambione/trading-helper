import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import discord_source as ds  # noqa: E402


def test_open_launch_captures_output_and_error(monkeypatch):
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        so = Path(args[args.index("--stdout") + 1])
        se = Path(args[args.index("--stderr") + 1])
        so.write_text("ALERT LINE\n")
        se.write_text("")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(ds.subprocess, "run", fake_run)
    out = ds._via_open([str(ds._OCR_APP_BIN), "--owner", "Discord"])
    assert out.returncode == 0 and out.stdout == "ALERT LINE\n"
    a = calls[0]
    assert a[:5] == ["open", "-W", "-n", "-g", "-a"] and a[5].endswith("DiscordOCR.app") and a[-2:] == ["--owner", "Discord"]


def test_open_launch_error_line_is_failure(monkeypatch):
    def fake_run(args, **kw):
        Path(args[args.index("--stderr") + 1]).write_text("[discord_ocr] ERROR: display capture failed\n")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(ds.subprocess, "run", fake_run)
    out = ds._via_open([str(ds._OCR_APP_BIN), "--owner", "Discord"])
    assert out.returncode == 1 and "ERROR" in out.stderr


def test_use_open_only_for_the_app_binary(monkeypatch):
    monkeypatch.delenv("DISCORD_OCR_DIRECT", raising=False)
    assert ds._use_open([str(ds._OCR_APP_BIN)]) == (sys.platform == "darwin")
    assert not ds._use_open(["swift", "x.swift"])
    monkeypatch.setenv("DISCORD_OCR_DIRECT", "1")
    assert not ds._use_open([str(ds._OCR_APP_BIN)])
