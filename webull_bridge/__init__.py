"""Webull bridge — serves Level-2 order-flow state and broker actions to
the Mobile Trader iPhone app.

Reuses the pure-math signal logic from webull-l2/l2_core.py (imbalance,
walls, playbook, LongView stance) but feeds it depth data from a provider
(mock simulator today, Webull OpenAPI once credentials are approved)
instead of screen OCR.
"""
