from conftest import LONG_SETUP, PARAMS, make_bars, uptrend_m5

from signals.smc import find_entry, find_swings, structure_bias


def test_swings_found_where_built():
    b = make_bars(LONG_SETUP)
    s = {(x.idx, x.kind) for x in find_swings(b, 2)}
    assert (4, "low") in s and (8, "high") in s and (12, "low") in s


def test_long_setup_fires_on_fvg_retest():
    sig = find_entry(make_bars(LONG_SETUP), "up", PARAMS)
    assert sig is not None and sig.side == "buy"
    assert sig.swept == 98 and sig.broken == 103
    assert sig.fvg == (100.5, 102.5)
    assert sig.entry == 103 and sig.sl < 97.6 and sig.tp > sig.entry
    assert abs((sig.tp - sig.entry) - PARAMS.reward_risk * (sig.entry - sig.sl)) < 1e-9


def test_no_long_without_up_bias():
    b = make_bars(LONG_SETUP)
    assert find_entry(b, None, PARAMS) is None
    assert find_entry(b, "down", PARAMS) is None


def test_setup_trades_once():
    rows = LONG_SETUP + [(103.4, 102.3, 103.1)]  # second touch of the FVG
    assert find_entry(make_bars(rows), "up", PARAMS) is None


def test_fvg_not_reached_yet_no_signal():
    rows = LONG_SETUP[:-1] + [(104.6, 103.2, 104)]
    assert find_entry(make_bars(rows), "up", PARAMS) is None


def test_short_is_exact_mirror():
    long_sig = find_entry(make_bars(LONG_SETUP), "up", PARAMS)
    mirrored = make_bars(LONG_SETUP).mirrored()
    short = find_entry(mirrored, "down", PARAMS)
    assert short is not None and short.side == "sell"
    assert short.entry == -long_sig.entry and short.sl == -long_sig.sl
    assert short.sl > short.entry > short.tp
    assert short.fvg == (-102.5, -100.5)


def test_stop_too_wide_is_skipped():
    from dataclasses import replace
    assert find_entry(make_bars(LONG_SETUP), "up", replace(PARAMS, max_sl_atr=0.5)) is None


def test_structure_bias():
    up = uptrend_m5()
    assert structure_bias(up, 3) == "up"
    assert structure_bias(up.mirrored(), 3) == "down"


def test_repriced_keeps_stop():
    sig = find_entry(make_bars(LONG_SETUP), "up", PARAMS).repriced(103.2, 2.0)
    assert sig.entry == 103.2 and abs(sig.tp - (103.2 + 2 * (103.2 - sig.sl))) < 1e-9
