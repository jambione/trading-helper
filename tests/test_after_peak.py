"""after_peak: the fixed pattern flags."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "studies"))
import after_peak as ap  # noqa: E402


def test_flags_peak_lower_and_slow_roll():
    hh = np.full(30, 10.0); cc = np.full(30, 9.98)
    hh[15] = 10.05                       # peak 5 bars before i=20
    cc[20] = 10.03                       # ~-20 bp below the peak
    slow = np.linspace(-40, -25, 30); slow[18:21] = [-25.0, -25.1, -25.2]   # rising, then rolling
    pl, roll = ap.flags(hh, cc, slow, 20)
    assert pl and roll
    cc2 = cc.copy(); cc2[20] = 10.049    # not 10 bp below the peak
    assert not ap.flags(hh, cc2, slow, 20)[0]
    hh2 = hh.copy(); hh2[19] = 10.10     # peak only 1 bar ago
    assert not ap.flags(hh2, cc, slow, 20)[0]
    assert not ap.flags(hh, cc, np.linspace(-40, -20, 30), 20)[1]   # still rising: no roll
