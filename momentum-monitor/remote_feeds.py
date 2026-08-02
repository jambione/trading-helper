"""Display-only feeds fed from the dashboard instead of polled locally.

AI research and the Stocktwits trending poll run on the server now
(ai_trader.py / trending_screener.py). Their rows arrive in the /api/state
payload the monitor already fetches every tick, so the panels need data
without any network work of their own.

These subclass the real feeds rather than reimplementing them: claude_panel()
and stocktwits_panel() call display_rows() and read .error/.last_ok/.model off
the object, and all of that — including the LOOK-badge and max_price logic,
which is desk-tunable and stays local — is inherited untouched. Only the
methods that would hit the network are stubbed out.
"""
from typing import Any

from ai_suggest import AiSuggestions
from stocktwits_trending import StocktwitsTrending


class _RemoteMixin:
    """Replaces the fetch clock with ingest() from a dashboard payload."""

    def refresh(self, now: float | None = None) -> bool:
        return False

    def refresh_quotes(self, now: float | None = None) -> bool:
        return False

    def refresh_volume(self, now: float | None = None, client=None) -> bool:
        return False

    # The dashboard serves {} for a screener that has never written its file.
    # That is not the same as a screener reporting an empty list, and the panel
    # must not read as "nothing is trending" when the truth is "nobody asked".
    _NOT_PUBLISHED = "not published by the dashboard — is the server job on?"

    def _ingest_common(self, payload: dict[str, Any] | None,
                       *, default_source: str | None = None) -> None:
        if not payload:
            self.rows = []
            self.by_symbol = {}
            self.last_ok = 0.0
            self.error = self._NOT_PUBLISHED
            return
        rows = list(payload.get("rows") or [])
        # Ensure every row carries A/X provenance for the desk Src column.
        try:
            from ai_suggest import (
                SOURCE_MARK,
                ai_source_mark,
                normalize_ai_source,
            )
            for r in rows:
                if not isinstance(r, dict):
                    continue
                # Merged rows already carry source_mark A / X / AX.
                if r.get("source_mark") in ("A", "X", "AX", "a", "x", "ax"):
                    r["source_mark"] = str(r["source_mark"]).upper()
                    if r["source_mark"] == "AX":
                        r["source"] = r.get("source") or "both"
                        r["agreement"] = True
                    continue
                if r.get("agreement") or r.get("source") in ("both", "ax"):
                    r["source"] = "both"
                    r["source_mark"] = "AX"
                    r["agreement"] = True
                    continue
                raw = r.get("source") or default_source
                src = normalize_ai_source(raw)
                if src == "unknown" and default_source:
                    src = normalize_ai_source(default_source)
                if src != "unknown":
                    r["source"] = src
                    r["source_mark"] = SOURCE_MARK.get(src) or ai_source_mark(src)
        except Exception:  # noqa: BLE001
            pass
        self.rows = rows
        self.by_symbol = {r["symbol"]: r for r in rows if r.get("symbol")}
        self.last_ok = payload.get("last_ok") or 0.0
        self.last_quote_ok = payload.get("last_quote_ok") or 0.0
        self.error = payload.get("error") or ""
        self.quotes_error = payload.get("quotes_error") or ""


class RemoteStocktwitsTrending(_RemoteMixin, StocktwitsTrending):
    def ingest(self, payload: dict[str, Any] | None,
               now: float | None = None) -> None:
        self._ingest_common(payload)


class RemoteAiSuggestions(_RemoteMixin, AiSuggestions):
    def ingest(self, payload: dict[str, Any] | None,
               now: float | None = None) -> None:
        # Default Anthropic when payload predates source tagging; Grok
        # publisher should set source=xai per row (or payload-level source).
        default = None
        if payload:
            default = payload.get("source") or payload.get("backend")
            if default:
                try:
                    from ai_suggest import source_from_backend
                    # backend strings (claude_cli / cli) → anthropic / xai
                    if str(default).lower() in (
                            "claude_cli", "claude", "cli", "grok_cli", "api"):
                        default = source_from_backend(str(default))
                except Exception:  # noqa: BLE001
                    pass
        self._ingest_common(payload, default_source=default or "anthropic")
        if not payload:
            return
        self.model = payload.get("model") or self.model
        self.last_report_path = payload.get("last_report_path") or ""
        self.last_trades = payload.get("last_trades") or []
        # Panel title only — this process never trades, whatever the server says.
        self.trading = bool(payload.get("trading", False))
        self.trading_mode = payload.get("trading_mode") or "off"
