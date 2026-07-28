"""
test_alert_sound.py — the desk's alert sound: per kind, throttled, single-source.

Background the tests encode: _beep() used to be Windows-only — a raw
winsound.Beep(880, 180) square wave — and had no macOS branch at all. So on the
Mac the desk runs on, the only audible alert was the notification banner, which
fires at most once per alert_notify_interval (180s) AND is skipped entirely
while the terminal is frontmost. In practice: silence.

Two properties matter beyond "it makes a noise":

  • one sound per alert. The banner is now explicitly silent because _beep owns
    audio; otherwise a single event strikes twice, and the volume/gap settings
    would not govern what you actually hear.
  • a global minimum gap ON TOP of Alerter's cooldown. That cooldown is keyed
    (kind, symbol), so twenty symbols bursting at the open are each entitled to
    fire at once — which is noise, not information.

Run:
    .venv/bin/python -m pytest tests/test_alert_sound.py -q
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "momentum-monitor"))

ms = pytest.importorskip("momentum_signal", reason="monitor deps (rich) required")


@pytest.fixture(autouse=True)
def _reset_gap(monkeypatch):
    """Each test starts with the global sound gap cleared."""
    monkeypatch.setattr(ms, "_last_sound_at", 0.0, raising=False)


@pytest.fixture()
def played(monkeypatch):
    """Capture what would have been played instead of making noise."""
    calls: list[tuple[str, float]] = []
    monkeypatch.setattr(ms, "_play", lambda path, volume: calls.append((path, volume)))
    monkeypatch.setattr(ms.threading, "Thread",
                        lambda target, args=(), daemon=None: type(
                            "T", (), {"start": lambda self: target(*args)})())
    monkeypatch.setattr(ms.sys, "platform", "darwin")
    return calls


# ── sound selection ───────────────────────────────────────────────────────────

def test_each_alert_kind_maps_to_its_own_sound():
    """A FOCUS firing and a symbol merely appearing must be distinguishable
    without looking at the screen."""
    kinds = ("new", "st_new", "mflow", "burst", "st_look", "focus", "buy")
    assert set(ms.ALERT_SOUNDS) == set(kinds)
    assert ms.sound_for("new") != ms.sound_for("focus")


def test_the_frequent_kinds_get_the_brief_soft_sounds():
    """new/st_new fire constantly; giving them a fanfare is how an alert tone
    becomes something you resent at 04:00."""
    assert ms.sound_for("new") == "Tink"
    assert ms.sound_for("st_new") == "Pop"
    assert ms.sound_for("focus") == "Hero"
    assert ms.sound_for("buy") == "Hero"


def test_the_jarring_system_sounds_are_left_unmapped():
    """Basso is macOS's error sound; Funk/Sosumi/Frog are the harsh end."""
    assert not {"Basso", "Funk", "Sosumi", "Frog"} & set(ms.ALERT_SOUNDS.values())


def test_an_unknown_kind_falls_back_rather_than_going_silent():
    assert ms.sound_for("something_new") == ms.DEFAULT_ALERT_SOUND


def test_a_single_kind_can_be_overridden_without_touching_the_rest():
    cfg = {"alert_sound_by_kind": {"burst": "Glass"}}
    assert ms.sound_for("burst", cfg) == "Glass"
    assert ms.sound_for("new", cfg) == "Tink"


def test_the_fallback_name_is_configurable():
    assert ms.sound_for("zzz", {"alert_sound_name": "Ping"}) == "Ping"


# ── playback ──────────────────────────────────────────────────────────────────

def test_a_beep_plays_the_sound_for_its_kind(played):
    ms._beep("focus", {})
    assert len(played) == 1
    assert played[0][0].endswith("/Hero.aiff")


def test_the_volume_is_well_under_full_by_default(played):
    ms._beep("new", {})
    assert played[0][1] == pytest.approx(0.35)
    assert played[0][1] < 1.0


def test_the_volume_is_clamped_to_a_sane_range(played):
    ms._beep("new", {"alert_sound_volume": 9.0})
    assert played[0][1] == 1.0
    played.clear()
    ms._last_sound_at = 0.0
    ms._beep("new", {"alert_sound_volume": "nonsense"})
    assert played[0][1] == pytest.approx(0.35)


def test_sound_can_be_turned_off_entirely(played):
    ms._beep("focus", {"alert_sound": False})
    assert played == []


def test_a_missing_sound_file_falls_back_instead_of_raising(played, monkeypatch):
    monkeypatch.setattr(ms.os.path, "exists",
                        lambda p: p.endswith(f"{ms.DEFAULT_ALERT_SOUND}.aiff"))
    ms._beep("burst", {"alert_sound_by_kind": {"burst": "NotAThing"}})
    assert played[0][0].endswith(f"/{ms.DEFAULT_ALERT_SOUND}.aiff")


# ── the global gap ────────────────────────────────────────────────────────────

def test_a_wide_burst_does_not_overlap_into_noise(played):
    """alert_cooldown is per (kind, symbol), so twenty symbols bursting at the
    open would each be entitled to fire simultaneously."""
    for sym in range(20):
        ms._beep("burst", {})
    assert len(played) == 1, "the min-gap should collapse a simultaneous burst"


def test_the_gap_is_configurable_and_zero_lets_everything_through(played):
    for _ in range(5):
        ms._beep("new", {"alert_sound_min_gap": 0.0})
    assert len(played) == 5


# ── one sound per alert ───────────────────────────────────────────────────────

def _fire(monkeypatch, kind="burst", **cfg):
    """Drive one alert with the banner forced to fire, capturing both paths."""
    beeps: list[tuple] = []
    banners: list[dict] = []
    monkeypatch.setattr(ms.sys, "platform", "darwin")
    monkeypatch.setattr(ms, "_beep", lambda k, c=None: beeps.append((k, c)))
    monkeypatch.setattr(ms, "_macos_notify",
                        lambda *a, **kw: banners.append(kw))
    base = {"alert_cooldown": 0.0, "desktop_toast": True,
            "alert_notify_interval": 0.0, "alert_only_when_hidden": False}
    base.update(cfg)
    ms.Alerter(base).fire(kind, "NVDA", "detail")
    return beeps, banners


def test_the_banner_is_silent_because_the_desk_owns_the_audio(monkeypatch):
    """Otherwise one event strikes twice, and alert_sound_volume /
    alert_sound_min_gap stop governing what is actually heard."""
    beeps, banners = _fire(monkeypatch)
    assert len(beeps) == 1 and len(banners) == 1
    assert banners[0].get("sound") is False


def test_the_alerter_passes_the_kind_through_so_sounds_differ_per_event(monkeypatch):
    beeps, _ = _fire(monkeypatch, kind="focus")
    assert beeps[0][0] == "focus"
    assert isinstance(beeps[0][1], dict), "the cfg must reach _beep for its knobs"


def test_the_notifier_names_its_sound_rather_than_taking_the_system_default(monkeypatch):
    """'default' is whatever is set in System Settings, so the banner and the
    desk would disagree about what an event sounds like."""
    cmds: list[list[str]] = []
    monkeypatch.setattr(ms.sys, "platform", "darwin")
    monkeypatch.setattr(ms.shutil, "which", lambda _n: "/opt/homebrew/bin/terminal-notifier")
    monkeypatch.setattr(ms.subprocess, "run",
                        lambda cmd, **kw: cmds.append(cmd))
    ms._macos_notify("t", "m", sound=True, auto_dismiss=0, sound_name="Glass")
    assert cmds and "-sound" in cmds[0]
    assert cmds[0][cmds[0].index("-sound") + 1] == "Glass"


def test_the_notifier_defaults_to_a_named_sound_not_the_system_one(monkeypatch):
    cmds: list[list[str]] = []
    monkeypatch.setattr(ms.sys, "platform", "darwin")
    monkeypatch.setattr(ms.shutil, "which", lambda _n: "/opt/homebrew/bin/terminal-notifier")
    monkeypatch.setattr(ms.subprocess, "run", lambda cmd, **kw: cmds.append(cmd))
    ms._macos_notify("t", "m", sound=True, auto_dismiss=0)
    assert cmds[0][cmds[0].index("-sound") + 1] == ms.DEFAULT_ALERT_SOUND


# ── config contract ───────────────────────────────────────────────────────────

def test_every_sound_knob_has_a_default():
    for key in ("alert_sound", "alert_sound_volume", "alert_sound_name",
                "alert_sound_min_gap", "alert_sound_by_kind"):
        assert key in ms.DEFAULTS, key


def test_the_windows_path_uses_a_system_alias_not_a_raw_square_wave(monkeypatch):
    """winsound.Beep drives the motherboard timer directly and is harsh; the
    system alias is whatever the OS considers a notification."""
    import types
    calls: list[tuple] = []
    fake = types.SimpleNamespace(
        PlaySound=lambda name, flags: calls.append(("PlaySound", name)),
        Beep=lambda f, d: calls.append(("Beep", f)),
        SND_ALIAS=1, SND_ASYNC=2)
    monkeypatch.setitem(sys.modules, "winsound", fake)
    monkeypatch.setattr(ms.sys, "platform", "win32")

    ms._beep("burst", {})
    assert calls == [("PlaySound", "SystemAsterisk")]
    assert not any(c[0] == "Beep" for c in calls)
