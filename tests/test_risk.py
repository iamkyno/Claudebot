from dataclasses import replace
from datetime import datetime, timezone

from conftest import LONG_SETUP, PARAMS, SPEC, T0, make_bars

from risk.guard import Context, RiskGuard, in_session, lot_size, sast_day_start
from signals.smc import find_entry

GOLD = [{"days": ["mon", "tue", "wed", "thu", "fri"], "start": "09:00", "end": "22:00"}]


def utc(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


def test_sessions_in_sast():
    assert in_session(utc(2026, 11, 24, 7, 0), GOLD)       # 09:00 SAST Tue
    assert not in_session(utc(2026, 11, 24, 6, 59), GOLD)  # 08:59 SAST
    assert not in_session(utc(2026, 11, 24, 20, 0), GOLD)  # 22:00 SAST
    assert not in_session(utc(2026, 11, 28, 10, 0), GOLD)  # Saturday
    assert in_session(utc(2026, 11, 28, 10, 0), "always")


def test_overnight_window():
    w = [{"days": ["mon"], "start": "22:00", "end": "02:00"}]
    assert in_session(utc(2026, 11, 23, 21, 0), w)   # Mon 23:00 SAST
    assert in_session(utc(2026, 11, 23, 23, 30), w)  # Tue 01:30 SAST
    assert not in_session(utc(2026, 11, 24, 21, 0), w)


def test_sast_day_rolls_at_local_midnight():
    day, start = sast_day_start(utc(2026, 11, 24, 22, 30))  # 00:30 SAST on the 25th
    assert day == "2026-11-25" and start == utc(2026, 11, 24, 22, 0)


def test_lot_size_risks_the_percentage():
    # 1% of 10k = $100; stop 5.00 away = 500 ticks * $1 = $500/lot -> 0.20 lots
    assert lot_size(10_000, 1.0, 2000.0, 1995.0, SPEC) == 0.2
    # rounds down, never up
    assert lot_size(10_000, 1.0, 2000.0, 1994.0, SPEC) == 0.16
    # below the broker minimum -> skip rather than oversize
    assert lot_size(100, 0.1, 2000.0, 1990.0, SPEC) == 0.0


def _ctx(**kw):
    base = dict(now_utc=T0, bid=102.95, ask=103.0, equity=10_000, day_start_equity=10_000,
                trades_today=0, open_positions=0)
    base.update(kw)
    return Context(**base)


def _sig():
    return find_entry(make_bars(LONG_SETUP), "up", PARAMS)


def test_guard_allows_clean_setup(tmp_path):
    assert RiskGuard({}, tmp_path).evaluate(_sig(), _ctx(), GOLD, SPEC).ok


def test_guard_blocks(tmp_path):
    g = RiskGuard({"max_trades_per_day": 2, "max_daily_loss_pct": 2.0}, tmp_path)
    sig = _sig()
    assert "trades" in g.evaluate(sig, _ctx(trades_today=2), GOLD, SPEC).reason
    assert "daily loss" in g.evaluate(sig, _ctx(equity=9_790), GOLD, SPEC).reason
    assert "hours" in g.evaluate(sig, _ctx(now_utc=utc(2026, 11, 24, 21)), GOLD, SPEC).reason
    assert "already open" in g.evaluate(sig, _ctx(open_positions=1), GOLD, SPEC).reason
    assert "spread" in g.evaluate(sig, _ctx(ask=104.5), GOLD, SPEC).reason
    tight = replace(SPEC, stops_level=100_000)
    assert "broker" in g.evaluate(sig, _ctx(), GOLD, tight).reason
    (tmp_path / "KILL").touch()
    assert "kill" in g.evaluate(sig, _ctx(), GOLD, SPEC).reason
