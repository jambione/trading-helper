"""Index/sector levered ETPs that arrive nameless from most-actives."""
from ticker_filters import is_levered_etp


def test_nameless_index_levered_pairs_blocked():
    for s in ("SOXS", "SOXL", "TQQQ", "SQQQ", "UVXY", "TZA", "LABU"):
        assert is_levered_etp(s), s


def test_common_stock_untouched():
    for s in ("SMCI", "IONQ", "RIOT", "BULL", "CIFR", "SOFI", "SH" + "OP"):
        assert not is_levered_etp(s), s
