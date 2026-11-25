from datetime import timedelta

import pytest
from conftest import LONG_SETUP, T0, FakeClient, make_bars, uptrend_m5

from bot.engine import Engine
from bot.notifier import Notifier
from execution.executor import DryRunExecutor, LiveExecutor, ModeError, check_mode
from journal.store import Journal

CFG = {
    "allow_live": True,
    "live_account_id": "123456",
    "symbols": {"XAUUSD": {"sessions": "always"}},
    "strategy": {"max_sl_atr": 10},
    "risk": {"risk_per_trade_pct": 1.0, "max_hold_minutes": 30},
}


def _engine(client, executor, tmp_path, now=T0 + timedelta(minutes=19), cfg=CFG):
    return Engine(cfg, client, executor, Journal(":memory:"), Notifier(False), root=tmp_path,
                  now=lambda: now)


def test_check_mode():
    check_mode("dry-run", "1", {})
    with pytest.raises(ModeError, match="allow_live"):
        check_mode("live", "123456", {"live_account_id": "123456"})
    with pytest.raises(ModeError, match="doesn't match"):
        check_mode("live", "999", {"allow_live": True, "live_account_id": "123456"})
    with pytest.raises(ModeError, match="not set"):
        check_mode("live", "123456", {"allow_live": True})
    with pytest.raises(ModeError):
        check_mode("demo", "123456", CFG)
    check_mode("live", "123456", CFG)


def test_dry_run_journals_but_sends_nothing(tmp_path):
    client = FakeClient(make_bars(LONG_SETUP), uptrend_m5())
    eng = _engine(client, DryRunExecutor(), tmp_path)
    eng.run_once()
    assert client.orders == []
    row = eng.journal.db.execute("SELECT * FROM orders").fetchone()
    assert row["status"] == "dry-run" and row["symbol"] == "XAUUSDm" and row["side"] == "buy"
    assert row["volume"] > 0
    eng.run_once()  # same bar again -> nothing new
    assert eng.journal.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1


def test_live_places_and_syncs_close(tmp_path):
    client = FakeClient(make_bars(LONG_SETUP), uptrend_m5())
    eng = _engine(client, LiveExecutor(client, CFG), tmp_path)
    eng.run_once()
    assert len(client.orders) == 1
    sym, side, vol, sl, tp = client.orders[0]
    assert side == "buy" and sl < 103.0 < tp
    client.open_positions.clear()  # SL/TP hit at the broker
    eng.run_once()
    row = eng.journal.db.execute("SELECT * FROM orders").fetchone()
    assert row["status"] == "closed" and row["profit"] == 12.5


def test_lot_cap_applies(tmp_path):
    client = FakeClient(make_bars(LONG_SETUP), uptrend_m5())
    cfg = {**CFG, "risk": {**CFG["risk"], "max_lots_per_trade": 0.01}}
    _engine(client, LiveExecutor(client, cfg), tmp_path, cfg=cfg).run_once()
    assert client.orders[0][2] == 0.01


def test_wrong_account_is_refused(tmp_path):
    client = FakeClient(make_bars(LONG_SETUP), uptrend_m5())
    eng = _engine(client, LiveExecutor(client, {**CFG, "live_account_id": "777"}), tmp_path)
    eng.run_once()
    assert client.orders == []
    assert eng.journal.db.execute("SELECT decision FROM signals").fetchone()["decision"] == "refused"


def test_api_error_marks_signal_failed(tmp_path):
    client = FakeClient(make_bars(LONG_SETUP), uptrend_m5())

    def boom(*a, **k):
        raise RuntimeError("HTTP 503")

    client.market_order = boom
    eng = _engine(client, LiveExecutor(client, CFG), tmp_path)
    eng.run_once()
    assert eng.journal.db.execute("SELECT decision FROM signals").fetchone()["decision"] == "failed"


def test_time_stop_closes_stale_position(tmp_path):
    client = FakeClient(make_bars(LONG_SETUP), uptrend_m5())
    j = Journal(":memory:")
    ex = LiveExecutor(client, CFG)
    Engine(CFG, client, ex, j, Notifier(False), root=tmp_path,
           now=lambda: T0 + timedelta(minutes=19)).run_once()
    assert len(client.open_positions) == 1
    Engine(CFG, client, ex, j, Notifier(False), root=tmp_path,
           now=lambda: T0 + timedelta(minutes=60)).run_once()
    assert client.open_positions == []


def test_kill_file_blocks(tmp_path):
    (tmp_path / "KILL").touch()
    client = FakeClient(make_bars(LONG_SETUP), uptrend_m5())
    eng = _engine(client, LiveExecutor(client, CFG), tmp_path)
    eng.run_once()
    assert client.orders == []
