"""Order-flow window math, OI tracker, grid sim, protective-stop paths."""

import json
import time

import numpy as np
import pandas as pd
import pytest

from backtest.optimize import ema_precompute, grid_simulate
from data.open_interest import OpenInterestTracker
from exchange.orders import OrderManager
from exchange.trade_flow import TradeFlowStream
from strategies.grid import GridStrategy


class TestTradeFlow:
    def _msg(self, sym, qty, buyer_is_maker):
        return json.dumps({"data": {"s": sym, "q": str(qty), "m": buyer_is_maker}})

    def test_flow_measures_aggressive_buy_share(self):
        tf = TradeFlowStream(["BTC/USDT"], window_seconds=60)
        # 3 aggressive buys of 2.0 (m=False), 1 aggressive sell of 2.0 (m=True)
        for _ in range(3):
            tf._on_message(None, self._msg("BTCUSDT", 2.0, False))
        tf._on_message(None, self._msg("BTCUSDT", 2.0, True))
        assert tf.flow("BTC/USDT") == pytest.approx(0.75)

    def test_flow_none_without_data(self):
        tf = TradeFlowStream(["BTC/USDT"])
        assert tf.flow("BTC/USDT") is None

    def test_old_trades_age_out_of_window(self):
        tf = TradeFlowStream(["BTC/USDT"], window_seconds=1)
        tf._on_message(None, self._msg("BTCUSDT", 5.0, False))
        time.sleep(1.1)
        assert tf.flow("BTC/USDT") is None


class TestOpenInterest:
    class FakeFut:
        def __init__(self):
            self.vals = iter([100.0, 110.0])
        def fetch_open_interest(self, perp):
            return {"openInterestAmount": next(self.vals)}

    class FakeClient:
        def __init__(self):
            self.futures = TestOpenInterest.FakeFut()

    def test_change_computed_between_snapshots(self):
        oi = OpenInterestTracker(self.FakeClient(), window_seconds=3600, fetch_ttl=0)
        assert oi.change("BTC/USDT") is None      # first snapshot only
        assert oi.change("BTC/USDT") == pytest.approx(0.10)   # 100 -> 110


class TestGridTunables:
    def test_overrides(self):
        g = GridStrategy({"spacing_mult": 0.8, "stop_mult": 6.0,
                          "tp_mult": 2.0, "adx_kill": 25})
        assert g.spacing_mult == 0.8 and g.stop_mult == 6.0
        assert g.tp_mult == 2.0 and g.adx_kill == 25

    def test_defaults_match_previous_behavior(self):
        g = GridStrategy({})
        assert (g.spacing_mult, g.stop_mult, g.tp_mult, g.adx_kill) == (0.5, 4.0, 1.0, 35)


class TestGridSim:
    def test_ranging_market_produces_trades(self):
        # Flat oscillating market: grid's home turf.
        bars = 3000
        rng = np.random.default_rng(5)
        t = np.arange(bars)
        close = 100 * np.exp(0.012 * np.sin(2*np.pi*t/60) + rng.normal(0, .001, bars))
        idx = pd.date_range("2024-01-01", periods=bars, freq="h")
        df = pd.DataFrame({"open": np.roll(close,1), "high": close*1.004,
                           "low": close*0.996, "close": close,
                           "volume": rng.uniform(5,30,bars)}, index=idx)
        df.iloc[0, df.columns.get_loc("open")] = close[0]
        F = ema_precompute(df)
        rets = grid_simulate(F, spacing_mult=0.5, stop_mult=4.0, tp_mult=1.0, adx_kill=45)
        assert len(rets) > 3


class TestProtectiveStopPaths:
    class FakeClient:
        def __init__(self):
            self.calls = []
            self.price = 100.0
        def fetch_ticker(self, s): return {"last": self.price}
        def get_min_order_amount(self, s, venue="spot"): return 0.0
        def place_protective_stop(self, *a, **k):
            self.calls.append(("place", a)); return {"id": "stop1"}
        def cancel_protective_stop(self, *a, **k):
            self.calls.append(("cancel", a))

    def test_paper_mode_never_touches_exchange_stops(self, monkeypatch):
        c = self.FakeClient()
        m = OrderManager(c, paper_mode=True, paper_balance=10_000.0,
                         fees_cfg={"futures_taker": 0.0005})
        monkeypatch.setattr(m, "_log_trade", lambda *a, **k: 1)
        monkeypatch.setattr(m, "_close_trade", lambda *a, **k: True)
        r = m.open_position("BTC/USDT", "buy", 1000.0, "t", 95, 110)
        m.update_stop(1, 97.0, symbol="BTC/USDT", side="buy", quantity=r["quantity"])
        m.close_position("BTC/USDT", r["quantity"], 1, side="buy")
        assert c.calls == []   # zero exchange-stop calls in paper mode
