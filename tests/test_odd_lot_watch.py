"""tools/odd_lot_watch.py: keep listed companies only, one line each, newest first."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import odd_lot_watch as W


def test_parse_and_filter():
    assert W.parse_name("PLAYSTUDIOS, Inc.  (MYPS)  (CIK 0001823878)") == ("PLAYSTUDIOS, Inc.", ["MYPS"], "0001823878")
    assert W.parse_name("Ares Private Markets Fund  (CIK 0001876006)")[1] == []
    assert W.common_ticker(["SQFT", "SQFTP", "SQFTW"]) == "SQFT" and W.common_ticker(["PRIF-PD", "PRIF-PK"]) is None
    rows = [{"adsh": "0001-26-1", "filed": "2026-11-12", "display": "PLAYSTUDIOS, Inc.  (MYPS)  (CIK 0001823878)"},
            {"adsh": "0001-26-2", "filed": "2026-09-01", "display": "Ares Private Markets Fund  (CIK 0001876006)"},
            {"adsh": "0001-26-3", "filed": "2026-08-01", "display": "PLAYSTUDIOS, Inc.  (MYPS)  (CIK 0001823878)"},
            {"adsh": "0001-26-4", "filed": "2026-10-21", "display": "INVE Technologies, Inc.  (INVE)  (CIK 0001036044)"}]
    d = W.deals(rows)
    assert [x["ticker"] for x in d] == ["MYPS", "INVE"] and d[0]["filed"] == "2026-11-12"
    assert d[0]["url"] == "https://www.sec.gov/Archives/edgar/data/1823878/0001261/"
    text = W.render(d, {"MYPS": 4.93}, "2026-11-15", {"INVE"})
    assert "NEW  MYPS" in text and "about $488" in text and "     INVE" in text and "no recent price" in text
    assert "No listed odd-lot offers" in W.render([], {}, "x", set())
