from datetime import timedelta

import pytest
from conftest import LONG_SETUP, T0, FakeClient, make_bars, uptrend_m5

from bot.engine import Engine
from bot.notifier import Notifier
from execution.executor import DryRunExecutor, ModeError, Mt5Executor, check_mode
from journal.store import Journal

CFG = {
    "symbols": {"XAUUSD": {"sessions": "always"}},
    "strategy": {"max_sl_atr": 10},
    "risk": {"risk_per_trade_pct": 1.0, "max_hold_minutes": 30},
}


def _engine(client, executor, tmp_path, now=T0 + timedelta(minutes=19)):
    return Engine(CFG, client, executor, Journal(":memory:"), Notifier(False), root=tmp_path,
                  now=lambda: now)


def test_check_mode():
    check_mode("dry-run", False, False)
    check_mode("demo", True, False)
    with pytest.raises(ModeError):
        check_mode("demo", False, False)
    with pytest.raises(ModeError):
        check_mode("live", False, False)
    check_mode("live", False, True)


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


def test_demo_places_and_syncs_close(tmp_path):
    client = FakeClient(make_bars(LONG_SETUP), uptrend_m5(), demo=True)
    eng = _engine(client, Mt5Executor(client, "demo", False), tmp_path)
    eng.run_once()
    assert len(client.orders) == 1
    sym, side, vol, sl, tp = client.orders[0]
    assert side == "buy" and sl < 103.0 < tp
    client.open_positions.clear()  # SL/TP hit at the broker
    eng.run_once()
    row = eng.journal.db.execute("SELECT * FROM orders").fetchone()
    assert row["status"] == "closed" and row["profit"] == 12.5


def test_demo_mode_refuses_real_account(tmp_path):
    client = FakeClient(make_bars(LONG_SETUP), uptrend_m5(), demo=True)
    eng = _engine(client, Mt5Executor(client, "demo", False), tmp_path)
    client.demo = False  # account switched to real while running
    eng.run_once()
    assert client.orders == []
    sig = eng.journal.db.execute("SELECT decision FROM signals").fetchone()
    assert sig["decision"] == "refused"


def test_time_stop_closes_stale_position(tmp_path):
    client = FakeClient(make_bars(LONG_SETUP), uptrend_m5(), demo=True)
    j = Journal(":memory:")
    ex = Mt5Executor(client, "demo", False)
    Engine(CFG, client, ex, j, Notifier(False), root=tmp_path,
           now=lambda: T0 + timedelta(minutes=19)).run_once()
    assert len(client.open_positions) == 1
    Engine(CFG, client, ex, j, Notifier(False), root=tmp_path,
           now=lambda: T0 + timedelta(minutes=60)).run_once()
    assert client.open_positions == []


def test_kill_file_blocks(tmp_path):
    (tmp_path / "KILL").touch()
    client = FakeClient(make_bars(LONG_SETUP), uptrend_m5(), demo=True)
    eng = _engine(client, Mt5Executor(client, "demo", False), tmp_path)
    eng.run_once()
    assert client.orders == []
