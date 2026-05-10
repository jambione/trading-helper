"""
windows_agent.py — Local Windows agent for Webull Desktop automation.

Run this on your Windows machine:
    python windows_agent.py   (or double-click windows_agent.bat)

Listens on http://localhost:8889
The trading dashboard's Add button POSTs here to automate Webull Desktop
locally — no server-side automation needed.

Endpoints:
    POST /add-wb        {"ticker": "NVDA"}   → add to Webull watchlist
    GET  /health        → {"ok": true, "platform": "win32"}
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

# ── Import workflow from transcription/workflows.py ───────────────────────────
# Add the transcription folder to the path so we can import workflows directly.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "transcription"))

from workflows import workflow_add_wb   # noqa: E402  (import after sys.path tweak)


# ── HTTP handler ──────────────────────────────────────────────────────────────

class AgentHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"[{self.address_string()}] {fmt % args}")

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self._json(200, {
                "ok":       True,
                "platform": sys.platform,
                "agent":    "windows_agent",
            })
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path

        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length) if length else b""
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid JSON"})
            return

        if path == "/add-wb":
            ticker = data.get("ticker", "").strip().upper()
            if not ticker:
                self._json(400, {"error": "missing ticker"})
                return

            print(f"  → add-wb: {ticker}")
            ok = workflow_add_wb(ticker)
            self._json(200 if ok else 500, {"ok": ok, "ticker": ticker})

        else:
            self._json(404, {"error": "not found"})

    def _json(self, code: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)


# ── Entry point ───────────────────────────────────────────────────────────────

PORT = 8889

if __name__ == "__main__":
    if sys.platform != "win32":
        print("⚠️  WARNING: Not running on Windows — Webull automation will be dry-run only.")
    server = HTTPServer(("127.0.0.1", PORT), AgentHandler)
    print(f"✅ Windows local agent running on http://localhost:{PORT}")
    print(f"   POST /add-wb  {{\"ticker\": \"NVDA\"}}  to add to Webull Desktop")
    print(f"   GET  /health  to check status")
    print(f"   Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
