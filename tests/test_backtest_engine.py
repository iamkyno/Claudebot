"""Backtester parity tests: fees, slippage, shorts, trailing, partial TP."""

import numpy as np
import pandas as pd
import pytest

from backtest.engine import BacktestEngine
from strategies.base import BaseStrategy, Signal


class OneShotLong(BaseStrategy):
    """Buys once at the first opportunity with a fixed bracket."""
    def __init__(self, stop=95.0, tp=110.0):
        super().__init__({})
        self.name = "oneshot"
        self.fired = False
        self._stop, self._tp = stop, tp

    def generate_signal(self, symbol, df, **kw):
        if self.fired:
            return None
        self.fired = True
        price = float(df.iloc[-1]["close"])
        return Signal(symbol=symbol, strategy=self.name, signal_type="buy",
                      confidence=0.7, stop_loss=self._stop, take_profit=self._tp,
                      features={})

    def should_exit(self, symbol, df, trade):
        return False


class OneShotShort(OneShotLong):
    def generate_signal(self, symbol, df, **kw):
        sig = super().generate_signal(symbol, df, **kw)
        if sig:
            sig.signal_type = "sell"
        return sig


def _ramp(start=100.0, end=120.0, bars=200):
    idx = pd.date_range("2024-01-01", periods=bars, freq="h")
    path = np.linspace(start, end, bars)
    return pd.DataFrame({
        "open": path, "high": path * 1.002, "low": path * 0.998,
        "close": path, "volume": np.full(bars, 10.0),
    }, index=idx)


class TestCosts:
    def test_fees_are_charged_and_reduce_pnl(self):
        strat = OneShotLong(stop=90, tp=112)
        eng = BacktestEngine(strat, fee_rate=0.001, slippage_bps=0,
                             trailing=False, partial_tp=False)
        r = eng.run("T/USDT", _ramp())
        assert r.total_trades == 1
        assert r.total_fees > 0
        t = r.trades[0]
        gross = (t.exit_price - t.entry_price) * (t.quantity)
        assert t.pnl < gross  # net strictly below gross

    def test_zero_cost_run_beats_costed_run(self):
        r_free = BacktestEngine(OneShotLong(90, 112), fee_rate=0.0, slippage_bps=0,
                                trailing=False, partial_tp=False).run("T", _ramp())
        r_cost = BacktestEngine(OneShotLong(90, 112), fee_rate=0.002, slippage_bps=10,
                                trailing=False, partial_tp=False).run("T", _ramp())
        assert r_free.total_pnl > r_cost.total_pnl


class TestShorts:
    def test_short_profits_in_downtrend(self):
        strat = OneShotShort(stop=200.0, tp=85.0)
        eng = BacktestEngine(strat, fee_rate=0.0005, slippage_bps=0,
                             trailing=False, partial_tp=False)
        r = eng.run("T/USDT", _ramp(start=100, end=80))
        assert r.total_trades == 1
        assert r.trades[0].pnl > 0

    def test_short_loses_in_uptrend(self):
        # Bracket far out of reach so the short rides the uptrend to the end
        # of data and settles at a loss.
        strat = OneShotShort(stop=500.0, tp=10.0)
        eng = BacktestEngine(strat, fee_rate=0.0005, slippage_bps=0,
                             trailing=False, partial_tp=False)
        r = eng.run("T/USDT", _ramp(start=100, end=130))
        assert r.total_trades == 1
        assert r.trades[0].pnl < 0


class TestExitMechanics:
    def test_trailing_stop_locks_gain_on_reversal(self):
        # Up 100→118 then crash to 80: without trailing the fixed stop at 90
        # gives back everything; with trailing the exit is far higher.
        bars = 300
        up = np.linspace(100, 118, bars // 2)
        down = np.linspace(118, 80, bars // 2)
        path = np.concatenate([up, down])
        idx = pd.date_range("2024-01-01", periods=bars, freq="h")
        df = pd.DataFrame({"open": path, "high": path * 1.002,
                           "low": path * 0.998, "close": path,
                           "volume": np.full(bars, 10.0)}, index=idx)

        no_trail = BacktestEngine(OneShotLong(90, 999), fee_rate=0, slippage_bps=0,
                                  trailing=False, partial_tp=False).run("T", df.copy())
        trail = BacktestEngine(OneShotLong(90, 999), fee_rate=0, slippage_bps=0,
                               trailing=True, partial_tp=False).run("T", df.copy())
        assert trail.trades[0].exit_price > no_trail.trades[0].exit_price
        assert trail.total_pnl > no_trail.total_pnl

    def test_partial_tp_banks_and_moves_to_breakeven(self):
        strat = OneShotLong(stop=95, tp=112)
        eng = BacktestEngine(strat, fee_rate=0, slippage_bps=0,
                             trailing=False, partial_tp=True,
                             tp1_ratio=0.5, tp1_close=0.5)
        r = eng.run("T/USDT", _ramp(100, 120))
        t = r.trades[0]
        assert t.tp1_filled
        assert t.pnl > 0
