"""Unit tests for traffic_log + login_log recording."""

from __future__ import annotations

import json
from pathlib import Path

import login_log
import traffic_log


class _FakeClient:
    def __init__(self, host="9.9.9.9"):
        self.host = host


class _FakeRequest:
    def __init__(self, headers=None, host="9.9.9.9"):
        self.headers = headers or {}
        self.client = _FakeClient(host)


def test_client_meta_prefers_cf_headers(tmp_path, monkeypatch):
    req = _FakeRequest({
        "CF-Connecting-IP": "203.0.113.10",
        "CF-IPCountry": "us",
        "user-agent": "Mozilla/5.0 Test",
        "CF-Ray": "abc-EWR",
        "X-Forwarded-For": "1.2.3.4",
    })
    meta = traffic_log.client_meta_from_request(req)
    assert meta["ip"] == "203.0.113.10"
    assert meta["cf_country"] == "US"
    assert meta["cf_ray"] == "abc-EWR"
    assert "Mozilla" in meta["user_agent"]


def test_record_page_and_skip_static(tmp_path, monkeypatch):
    log_file = tmp_path / "traffic_log.json"
    monkeypatch.setattr(traffic_log, "TRAFFIC_LOG_FILE", log_file)
    traffic_log._debounce.clear()

    # Skip static
    assert traffic_log.record_hit(path="/static/js/app.js", method="GET", ip="1.1.1.1") is False
    # Skip /api/state first? presence records first hit
    assert traffic_log.record_hit(path="/api/state", method="GET", ip="1.1.1.1") is True
    # Debounced presence
    assert traffic_log.record_hit(path="/api/state", method="GET", ip="1.1.1.1") is False

    assert traffic_log.record_hit(
        path="/", method="GET", ip="1.1.1.1", cf_country="US", user_agent="Chrome"
    ) is True
    assert traffic_log.record_hit(
        path="/api/tickers/add", method="POST", ip="1.1.1.1", username="jmb"
    ) is True

    rows = json.loads(log_file.read_text())
    assert len(rows) == 3
    kinds = {r["event"] for r in rows}
    assert "presence" in kinds
    assert "page" in kinds
    assert "action" in kinds

    summary = traffic_log.summarize(hours=24)
    assert summary["unique_visitors"] == 1
    assert summary["total_events"] == 3
    assert summary["visitors"][0]["ip"] == "1.1.1.1"


def test_login_log_records_cf_country(tmp_path, monkeypatch):
    log_file = tmp_path / "login_log.json"
    monkeypatch.setattr(login_log, "LOGIN_LOG_FILE", log_file)
    monkeypatch.setattr(login_log, "_geo_lookup", lambda ip: {
        "city": "Shelton", "region": "Connecticut", "country": "United States", "country_code": "US"
    })

    login_log.record_login("jmb", "32.1.2.3", "UA", success=True, cf_country="US")
    # Wait briefly for background geo thread
    import time
    time.sleep(0.3)

    rows = json.loads(log_file.read_text())
    assert len(rows) == 1
    assert rows[0]["username"] == "jmb"
    assert rows[0]["cf_country"] == "US"
    assert rows[0]["success"] is True
    # geo thread may have filled city
    loc = rows[0].get("location") or {}
    assert loc.get("country_code") in ("US", "United States") or loc.get("city") == "Shelton"
