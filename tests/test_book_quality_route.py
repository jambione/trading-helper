import json


def test_book_quality_route_reads_latest(tmp_path, monkeypatch):
    import dashboard
    from fastapi.testclient import TestClient
    p = tmp_path / "latest.json"
    p.write_text(json.dumps({"ts": "2026-11-19T10:00:00-05:00", "opens": {"last_10m": 1}}))
    monkeypatch.setattr(dashboard, "BOOK_QUALITY_PATH", p)
    import asyncio
    res = asyncio.run(dashboard.api_book_quality())
    body = json.loads(res.body)
    assert body["ok"] and body["opens"]["last_10m"] == 1 and "age_sec" in body
    monkeypatch.setattr(dashboard, "BOOK_QUALITY_PATH", tmp_path / "missing.json")
    assert json.loads(asyncio.run(dashboard.api_book_quality()).body)["ok"] is False


def test_book_quality_is_not_public():
    import dashboard
    assert not any("/api/book-quality".startswith(p) for p in dashboard._PUBLIC_PREFIX)
