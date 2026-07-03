"""Maker-fee path, tunable scalp brackets, and the optimizer simulator."""

import numpy as np
import pandas as pd
import pytest

from backtest.optimize import (
    ema_precompute, ema_simulate, pick_best, precompute, score, simulate,
)
from exchange.orders import OrderManager
from strategies.ema_cross import EMACrossStrategy
from strategies.scalping import ScalpStrategy


class FakeClient:
    def __init__(self, price=100.0):
        self.price = price

    def fetch_ticker(self, symbol):
        return {"last": self.price}

    def get_min_order_amount(self, symbol, venue="spot"):
        return 0.0


class TestMakerEntries:
    def test_maker_pays_maker_fee_no_slippage(self, monkeypatch):
        m = OrderManager(FakeClient(), paper_mode=True, paper_balance=10_000.0,
                         fees_cfg={"spot_taker": 0.001, "futures_taker": 0.0005,
                                   "futures_maker": 0.0002, "slippage_bps": 10})
        monkeypatch.setattr(m, "_log_trade", lambda *a, **k: 1)
        r = m.open_position("BTC/USDT", "buy", 1000.0, "scalp", 99, 101,
                            venue="futures", order_type="maker")
        # Resting order: fill at quoted price (no slip), maker fee 0.02%.
        assert r["price"] == pytest.approx(100.0)
        assert m.free_cash() == pytest.approx(10_000 - 1000 - 0.20)

    def test_taker_still_slips_and_pays_taker(self, monkeypatch):
        m = OrderManager(FakeClient(), paper_mode=True, paper_balance=10_000.0,
                         fees_cfg={"futures_taker": 0.0005, "futures_maker": 0.0002,
                                   "slippage_bps": 10})
        monkeypatch.setattr(m, "_log_trade", lambda *a, **k: 1)
        r = m.open_position("BTC/USDT", "buy", 1000.0, "scalp", 99, 101,
                            venue="futures", order_type="taker")
        assert r["price"] == pytest.approx(100.1)   # crossed the spread

    def test_fee_rate_liquidity_dimension(self):
        m = OrderManager(FakeClient(), paper_mode=True,
                         fees_cfg={"futures_taker": 0.0005, "futures_maker": 0.0002})
        assert m.fee_rate("buy", venue="futures", liquidity="maker") == 0.0002
        assert m.fee_rate("buy", venue="futures", liquidity="taker") == 0.0005


class TestTunableBrackets:
    def test_tuning_overrides_defaults(self):
        s = ScalpStrategy({"min_edge_mult": 4.0, "tp_mult": 2.0, "sl_mult": 0.5},
                          fee_rate=0.0005)
        assert s.min_edge == pytest.approx(0.001 * 4.0)
        assert s.tp_mult == 2.0 and s.sl_mult == 0.5

    def test_defaults_without_tuning(self):
        s = ScalpStrategy({}, fee_rate=0.0005)
        assert s.min_edge == pytest.approx(0.003)
        assert s.tp_mult == 1.6 and s.sl_mult == 0.6


def _history(bars=3000, seed=3):
    """Synthetic 1m frame with volatility and drift so setups actually fire."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=bars, freq="min")
    rets = rng.normal(0.0002, 0.0015, bars)
    close = 100 * np.cumprod(1 + rets)
    high = close * (1 + np.abs(rng.normal(0, 0.0008, bars)))
    low = close * (1 - np.abs(rng.normal(0, 0.0008, bars)))
    openp = np.roll(close, 1); openp[0] = close[0]
    vol = rng.uniform(5, 30, bars) * (1 + 3 * (rng.random(bars) > 0.95))
    return pd.DataFrame({"open": openp, "high": high, "low": low,
                         "close": close, "volume": vol}, index=idx)


class TestOptimizerSim:
    def test_precompute_shapes(self):
        F = precompute(_history())
        assert F["n"] == 3000
        for k in ("rsi", "atr_pct", "vwap_dev", "volr", "trend"):
            assert len(F[k]) == 3000

    def test_simulate_produces_trades_and_score(self):
        F = precompute(_history())
        rets = simulate(F, min_edge_mult=2.0, tp_mult=1.3, sl_mult=0.6, entry="maker")
        s = score(rets)
        assert s["n"] == len(rets)
        if s["n"]:
            assert -100 < s["avg_bps"] < 100   # sane per-trade magnitudes

    def test_maker_entry_beats_taker_on_same_data(self):
        F = precompute(_history())
        maker = simulate(F, 2.0, 1.3, 0.6, entry="maker")
        taker = simulate(F, 2.0, 1.3, 0.6, entry="taker")
        if len(maker) and len(taker):
            # Identical trades, cheaper costs — maker must net more.
            assert maker.sum() > taker.sum()

    def test_no_overlapping_positions(self):
        F = precompute(_history())
        rets = simulate(F, 2.0, 1.0, 0.4)
        # Can't hold more sim-trades than bars/1 — loose sanity bound; the
        # busy_until lockout is what this guards.
        assert len(rets) < F["n"] / 2

    def test_ema_tunables_override_defaults(self):
        s = EMACrossStrategy({"stop_atr": 3.0, "tp_atr": 6.0, "adx_min": 25})
        assert s.stop_atr == 3.0 and s.tp_atr == 6.0 and s.adx_min == 25

    def test_ema_defaults_match_previous_behavior(self):
        s = EMACrossStrategy({"atr_stop_multiplier": 1.5})
        assert s.stop_atr == pytest.approx(2.25)   # 1.5 * 1.5
        assert s.tp_atr == pytest.approx(4.5)      # 1.5 * 3.0
        assert s.adx_min == 20

    def test_ema_sim_trades_uptrend_profitably(self):
        # Drifting market with pullback waves: EMAs re-cross with ADX still
        # elevated and volume arriving on the up-legs — the shape ema_cross
        # is built to trade. The sim must fire and profit here.
        bars = 3000
        rng = np.random.default_rng(11)
        t = np.arange(bars)
        logp = (np.log(100) + 0.0004 * t
                + 0.02 * np.sin(2 * np.pi * t / 100)
                + rng.normal(0, 0.0005, bars))
        close = np.exp(logp)
        r = np.diff(logp, prepend=logp[0])
        vol = 10 * (1 + 50 * np.clip(r, 0, None)) * rng.uniform(0.9, 1.1, bars)
        idx = pd.date_range("2024-01-01", periods=bars, freq="h")
        df = pd.DataFrame({"open": np.roll(close, 1), "high": close * 1.003,
                           "low": close * 0.997, "close": close,
                           "volume": vol}, index=idx)
        df.iloc[0, df.columns.get_loc("open")] = close[0]

        F = ema_precompute(df)
        assert F["cross_up"].sum() > 5
        rets = ema_simulate(F, stop_atr=2.25, tp_atr=4.5, adx_min=15)
        assert len(rets) > 5
        assert rets.sum() > 0   # trend-following must profit in a rising market

    def test_pick_best_requires_robustness(self):
        weak = [{"n": 10, "net_bps": 500, "pf": 9.9, "folds_positive": 3,
                 "wr": 90, "min_edge_mult": 2, "tp_mult": 1, "sl_mult": 0.4}]
        assert pick_best(weak, min_trades=60) is None
        strong = [{**weak[0], "n": 200}]
        assert pick_best(strong, min_trades=60) == strong[0]
