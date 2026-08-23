"""The live catalyst read sits inside a 2-second entry poll.

Two things have to hold or it is worse than not having it: it must never
raise, and it must never confuse "no catalyst" with "nobody looked". The
second is the subtle one — a dead refresher and a quiet market produce the
same zero, and only the cache age tells them apart.
"""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

nf = pytest.importorskip("news_feed")

T = 1_787_000_000.0


def _n(offset_sec, headline="Something happened"):
    return {"ts": T + offset_sec, "headline": headline}


# ------------------------------------------------------------ point in time

def test_future_headlines_are_invisible():
    f = nf.features_at([_n(+1), _n(+600), _n(+86400)], T)
    assert f["has_news_24h"] is False
    assert f["n_news_24h"] == 0
    assert f["mins_since"] is None


def test_a_headline_at_the_exact_instant_does_not_count():
    assert nf.features_at([_n(0)], T)["has_news_24h"] is False


def test_prior_headlines_are_counted_and_timed():
    f = nf.features_at([_n(-3600), _n(-600), _n(+60)], T)
    assert f["n_news_24h"] == 2
    assert f["mins_since"] == pytest.approx(10.0)


def test_older_than_a_day_is_not_todays_catalyst_but_is_still_timed():
    f = nf.features_at([_n(-30 * 3600)], T)
    assert f["has_news_24h"] is False
    assert f["mins_since"] == pytest.approx(30 * 60.0)


def test_keyword_tags_read_only_prior_headlines():
    future = [_n(+60, "Announces $50M registered direct offering")]
    assert nf.features_at(future, T)["bearish"] is False
    past = [_n(-60, "Announces $50M registered direct offering")]
    assert nf.features_at(past, T)["bearish"] is True


def test_a_headline_with_no_timestamp_cannot_inform_anything():
    assert nf.features_at([{"headline": "no ts"}], T)["n_news_24h"] == 0


# ------------------------------------------------------------ absence

def test_an_unknown_symbol_reads_all_none_not_all_false(tmp_path, monkeypatch):
    """The whole point. False means 'we looked and there was nothing'."""
    monkeypatch.setattr(nf, "CACHE_PATH", tmp_path / "none.json")
    nf._CACHE["mtime"] = None
    f = nf.features_for("NOPE", T)
    assert f["n_news_24h"] is None
    assert f["has_news_24h"] is None
    assert f["bearish"] is None


def test_a_known_symbol_with_no_recent_news_reads_false_not_none(tmp_path,
                                                                 monkeypatch):
    p = tmp_path / "cache.json"
    p.write_text(json.dumps({"TEM": [_n(-30 * 3600)]}), encoding="utf-8")
    monkeypatch.setattr(nf, "CACHE_PATH", p)
    nf._CACHE["mtime"] = None
    f = nf.features_for("TEM", T)
    assert f["has_news_24h"] is False      # looked, found nothing fresh
    assert f["n_news_24h"] == 0


def test_a_missing_cache_has_no_age():
    import pathlib
    old = nf.CACHE_PATH
    try:
        nf.CACHE_PATH = pathlib.Path("/nonexistent/never/news.json")
        assert nf.cache_age_sec() is None
    finally:
        nf.CACHE_PATH = old


def test_a_corrupt_cache_is_empty_rather_than_fatal(tmp_path, monkeypatch):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(nf, "CACHE_PATH", p)
    nf._CACHE["mtime"] = None
    assert nf.load_cache() == {}
    assert nf.features_for("TEM", T)["n_news_24h"] is None


def test_a_cache_that_is_not_a_dict_is_rejected(tmp_path, monkeypatch):
    p = tmp_path / "list.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    monkeypatch.setattr(nf, "CACHE_PATH", p)
    nf._CACHE["mtime"] = None
    assert nf.load_cache() == {}


def test_features_for_never_raises_on_junk():
    for bad in (None, "", 12345, object()):
        assert nf.features_for(bad, T)["n_news_24h"] is None


def test_refresh_with_no_symbols_does_nothing():
    assert nf.refresh([]) == 0
    assert nf.refresh(None) == 0


# ------------------------------------------------------------ the entry hook

def test_the_entry_hook_returns_the_full_field_set_even_when_broken(monkeypatch):
    """ai_entry_watch must write a row whatever news_feed does."""
    import ai_entry_watch as ew

    def boom(*a, **k):
        raise RuntimeError("news is down")

    monkeypatch.setattr(nf, "features_for", boom)
    f = ew._news_fields("TEM", T)
    assert set(f) >= {"n_news_24h", "mins_since", "bearish", "bullish",
                      "cache_age_sec"}
    assert f["n_news_24h"] is None
