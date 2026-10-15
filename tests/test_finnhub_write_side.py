"""2026-10-15: a Finnhub socket that reads but cannot write must reconnect."""
from types import SimpleNamespace

import finnhub_stream as fs


def _ws(conn_lost=0, closing=False):
    return SimpleNamespace(transport=SimpleNamespace(_conn_lost=conn_lost, is_closing=lambda: closing))


def test_lost_writes_or_closing_transport_trigger_reconnect():
    assert not fs.write_side_lost(_ws())
    assert fs.write_side_lost(_ws(conn_lost=1))
    assert fs.write_side_lost(_ws(closing=True))
    assert not fs.write_side_lost(SimpleNamespace())          # no transport attribute: no false alarm


def test_check_runs_on_the_idle_tick_before_the_silence_kill():
    src = open(fs.__file__, encoding="utf-8").read()
    i = src.index("except asyncio.TimeoutError:")
    assert src.index("write_side_lost(ws)", i) < src.index("silence_limit > 0", i)
