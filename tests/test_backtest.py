from datetime import timedelta

from conftest import LONG_SETUP, PARAMS, T0, make_bars

from backtest.run import load_csv, report, simulate, to_m5
from signals.smc import Bars


def _history():
    """Rising M1 warm-up (gives an M5 up-bias), then the long setup, then a rally to TP."""
    rows, p = [], 80.0
    for i in range(260):
        p += 0.12 if i % 50 < 30 else -0.1  # M5-sized swings, net up
        rows.append((p + 0.3, p - 0.3, p))
    shift = rows[-1][2] - LONG_SETUP[0][2] + 1.0
    rows += [(h + shift, l + shift, c + shift) for h, l, c in LONG_SETUP]
    last = rows[-1][2]
    rows += [(last + 1.5 * k + 0.5, last + 1.5 * k - 0.3, last + 1.5 * k) for k in range(1, 12)]
    return make_bars(rows, T0 - timedelta(minutes=260))


def test_to_m5_buckets():
    b = make_bars([(1, 0, 0.5)] * 12, T0)
    m5, last = to_m5(b)
    assert len(m5) == 3 and last == [4, 9, 11]


def test_simulate_takes_the_setup_and_hits_tp():
    trades = simulate(_history(), PARAMS, 0.05, "always", {}, entry_bars=150, trend_bars=300)
    assert trades, "expected the built setup to trade"
    tr = trades[-1]
    assert tr.side == "buy" and tr.outcome == "tp" and tr.r > 1.5
    assert "Trades" in report(trades)


def test_wide_spread_skips():
    assert simulate(_history(), PARAMS, 50.0, "always", {}) == []


def test_load_mt5_export(tmp_path):
    f = tmp_path / "x.csv"
    f.write_text("<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>\n"
                 "2026.11.24\t10:00:00\t1.0\t2.0\t0.5\t1.5\t10\t0\t20\n")
    b: Bars = load_csv(str(f))
    assert b.time[0] == T0 and b.high[0] == 2.0
