import os
import sys
import re
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

def test_style_badge_css_present():
    css_path = os.path.join(os.path.dirname(__file__), "..", "static", "css", "styles.css")
    assert os.path.exists(css_path)
    with open(css_path, "r", encoding="utf-8") as f:
        content = f.read()
    assert ".style-badge" in content
    assert ".style-badge--thrust" in content
    assert ".style-badge--maker" in content
    assert ".style-badge--main" in content

def test_style_badge_js_function_present():
    js_path = os.path.join(os.path.dirname(__file__), "..", "static", "js", "feeds.js")
    assert os.path.exists(js_path)
    with open(js_path, "r", encoding="utf-8") as f:
        content = f.read()
    assert "function _bookStyleBadge(r)" in content
    assert "style-badge--thrust" in content
    assert "style-badge--maker" in content
    assert "style-badge--main" in content
    assert "_bookStyleBadge(r)" in content
