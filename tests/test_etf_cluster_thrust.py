import numpy as np
import pytest
from tools.studies.etf_cluster_thrust import (
    r1, vol_ratio, trade, first_true, evaluate
)


def test_r1():
    c = np.array([100.0, 101.0, 100.0])
    res = r1(c)
    assert np.isnan(res[0])
    assert pytest.approx(res[1], 1e-5) == 0.01
    assert pytest.approx(res[2], 1e-5) == -0.00990099


def test_vol_ratio():
    v = np.ones(20)
    v[15] = 3.0
    vr = vol_ratio(v, 15, k=15, need=10)
    assert pytest.approx(vr, 1e-5) == 3.0


def test_trade():
    o = np.zeros(390)
    c = np.zeros(390)
    o[16] = 100.0
    c[18] = 100.50
    # Hold H=3: t=15, entry open at t+1=16, exit close at t+3=18
    # return = (100.5 / 100.0 - 1) * 1e4 - 1.5 bp = 50.0 - 1.5 = 48.5 bp
    net = trade(o, c, 15, 3, cost=1.5)
    assert pytest.approx(net, 1e-4) == 48.5


def test_first_true():
    ts = [10, 12, 45, 46, 80]
    kept = first_true(ts, refract=30)
    assert kept == [10, 45, 80]


def test_evaluate_underpowered():
    rows = []
    res = evaluate(rows)
    assert res["status"] == "UNDERPOWERED"
