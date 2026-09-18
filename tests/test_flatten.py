"""The flatten switch — credential resolution, ordering, and verified-flat.

This is the script you reach for when the desk is dead and a position has no
stop, so its failure modes matter more than most: reporting success on a book
that is not flat, or closing before cancels settle, are the two that already
bit the desk's own liquidate_all (2026-08-07, USAR into the weekend naked).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "tools"))
sys.path.insert(0, str(_ROOT))

import flatten

# ── Fakes ─────────────────────────────────────────────────────────────────────

def _pos(symbol, qty=10, value=100.0, pl=0.0):
    return SimpleNamespace(symbol=symbol, qty=qty, market_value=value,
                           unrealized_pl=pl)


class FakeClient:
    """Broker that actually holds state, so 'verified flat' means something."""

    def __init__(self, positions=None, orders=None, *, market_open=True,
                 close_fails=0, cancel_raises=False):
        self.positions = list(positions or [])
        self.orders = list(orders or [])
        self.market_open = market_open
        self.close_fails = close_fails
        self.cancel_raises = cancel_raises
        self.calls: list[str] = []

    def get_account(self):
        return SimpleNamespace(account_number="PA123", equity="2439.54")

    def get_clock(self):
        return SimpleNamespace(is_open=self.market_open)

    def get_all_positions(self):
        return list(self.positions)

    def get_orders(self, filter=None):
        return list(self.orders)

    def cancel_orders(self):
        self.calls.append("cancel")
        if self.cancel_raises:
            raise RuntimeError("cancel boom")
        self.orders = []

    def close_position(self, symbol):
        self.calls.append(f"close:{symbol}")
        if self.close_fails > 0:
            self.close_fails -= 1
            raise RuntimeError("available:0 — held_for_orders")
        self.positions = [p for p in self.positions if p.symbol != symbol]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(flatten.time, "sleep", lambda *_: None)


@pytest.fixture
def _args(monkeypatch):
    def _set(**over):
        argv = ["flatten.py"]
        for k, v in over.items():
            if v is True:
                argv.append("--" + k.replace("_", "-"))
        monkeypatch.setattr(sys, "argv", argv)
    return _set


# ── It does not depend on the desk ────────────────────────────────────────────

def test_imports_nothing_from_the_desk():
    """The whole point: it must run when the desk does not.

    A stray `import config` or `import ai_positions` would reintroduce the
    dependency this script exists to avoid.
    """
    src = (_ROOT / "tools" / "flatten.py").read_text(encoding="utf-8")
    banned = ("import config", "from config import",
              "import ai_positions", "import ai_entry_watch",
              "import ai_trader", "import alpaca_trader", "import ai_trading")
    hits = [b for b in banned if b in src]
    assert not hits, f"flatten.py must not import the desk: {hits}"


# ── Credentials ───────────────────────────────────────────────────────────────

def test_env_wins_over_files(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "envkey")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "envsec")
    assert flatten.resolve_credentials() == ("envkey", "envsec", "environment")


def test_falls_back_to_secrets_json_then_engine_env(monkeypatch, tmp_path):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    monkeypatch.setattr(flatten, "_ROOT", tmp_path)

    # Neither file present.
    assert flatten.resolve_credentials() == ("", "", "nowhere")

    # signal_engine.env only — the laptop layout.
    (tmp_path / "signal_engine.env").write_text(
        "# comment\nALPACA_API_KEY=ek\nALPACA_SECRET_KEY=es\n")
    assert flatten.resolve_credentials() == ("ek", "es", "signal_engine.env")

    # secrets.json present too — the mini layout, and it wins.
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "secrets.json").write_text(
        json.dumps({"api_key": "sk", "secret_key": "ss"}))
    assert flatten.resolve_credentials() == ("sk", "ss", "config/secrets.json")


def test_no_credentials_exits_2(monkeypatch, tmp_path):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    monkeypatch.setattr(flatten, "_ROOT", tmp_path)
    with pytest.raises(SystemExit) as e:
        flatten.connect(paper=True)
    assert e.value.code == 2


# ── Ordering: cancel, settle, then close ──────────────────────────────────────

def test_cancels_before_closing():
    """A resting order holds the shares; closing first is refused available:0."""
    client = FakeClient(positions=[_pos("VEEA")],
                        orders=[SimpleNamespace(symbol="VEEA", id="o1")])
    errors: list[str] = []
    flatten._close_everything(client, errors)
    assert client.calls[0] == "cancel"
    assert "close:VEEA" in client.calls
    assert errors == []


def test_retries_a_close_that_loses_the_race_with_the_cancel():
    """The USAR shape: first close refused, cancel then completes, retry works."""
    client = FakeClient(positions=[_pos("USAR")],
                        orders=[SimpleNamespace(symbol="USAR", id="o1")],
                        close_fails=1)
    errors: list[str] = []
    closed = flatten._close_everything(client, errors)
    assert closed == ["USAR"]
    assert client.calls.count("close:USAR") == 2
    assert errors == [], "a recovered retry must not report an error"


def test_gives_up_after_the_attempt_budget_and_records_it():
    client = FakeClient(positions=[_pos("STUCK")], close_fails=99)
    errors: list[str] = []
    closed = flatten._close_everything(client, errors)
    assert closed == []
    assert len(errors) == 1 and "STUCK" in errors[0]


def test_a_failed_cancel_does_not_stop_the_close():
    """Cancel failing is a reason to try harder, not to leave shares naked."""
    client = FakeClient(positions=[_pos("VEEA")],
                        orders=[SimpleNamespace(symbol="VEEA", id="o1")],
                        cancel_raises=True)
    errors: list[str] = []
    closed = flatten._close_everything(client, errors)
    assert closed == ["VEEA"]
    assert any("cancel_orders" in e for e in errors)


# ── Exit status is the contract ───────────────────────────────────────────────

def test_flat_book_exits_0(monkeypatch, _args):
    _args(yes=True)
    monkeypatch.setattr(flatten, "connect", lambda paper: FakeClient())
    assert flatten.main() == 0


def test_successful_flatten_exits_0(monkeypatch, _args):
    _args(yes=True)
    client = FakeClient(positions=[_pos("VEEA"), _pos("NAMI")])
    monkeypatch.setattr(flatten, "connect", lambda paper: client)
    assert flatten.main() == 0
    assert client.positions == []


def test_a_position_left_open_exits_1(monkeypatch, _args):
    """Verified against the broker, not inferred from what was attempted.

    liquidate_all once returned ok:true with closed:0 because one cancel had
    succeeded, and a naked position went into the weekend.
    """
    _args(yes=True)
    client = FakeClient(positions=[_pos("STUCK")], close_fails=99)
    monkeypatch.setattr(flatten, "connect", lambda paper: client)
    assert flatten.main() == 1


def test_dry_run_sends_nothing_and_exits_0(monkeypatch, _args):
    _args(dry_run=True, yes=True)
    client = FakeClient(positions=[_pos("VEEA")],
                        orders=[SimpleNamespace(symbol="VEEA", id="o1")])
    monkeypatch.setattr(flatten, "connect", lambda paper: client)
    assert flatten.main() == 0
    assert client.calls == [], "--dry-run must not touch the broker"
    assert client.positions, "--dry-run must leave the book alone"


def test_non_interactive_without_yes_refuses(monkeypatch, _args):
    """Over ssh there is no tty, so an accidental invocation must not fire."""
    _args()
    client = FakeClient(positions=[_pos("VEEA")])
    monkeypatch.setattr(flatten, "connect", lambda paper: client)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    assert flatten.main() == 2
    assert client.calls == []


def test_paper_is_the_default(monkeypatch, _args):
    """--live is a separate, deliberate flag."""
    _args(yes=True)
    seen = {}

    def _connect(paper):
        seen["paper"] = paper
        return FakeClient()

    monkeypatch.setattr(flatten, "connect", _connect)
    flatten.main()
    assert seen["paper"] is True

    _args(live=True, yes=True)
    flatten.main()
    assert seen["paper"] is False


def test_closed_market_still_submits_and_says_so(monkeypatch, _args, capsys):
    _args(yes=True)
    client = FakeClient(positions=[_pos("VEEA")], market_open=False)
    monkeypatch.setattr(flatten, "connect", lambda paper: client)
    assert flatten.main() == 0
    out = capsys.readouterr().out
    assert "Market is closed" in out
    assert "close:VEEA" in client.calls
