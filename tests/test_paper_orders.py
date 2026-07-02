"""
Paper-wallet money-path tests, DB-free.

DB writes are stubbed out; what's under test is the wallet arithmetic:
fees, slippage, side-aware settlement, and the round-trip identity
    final_cash == start + net_pnl   (when flat)
"""

import pytest

from exchange.orders import OrderManager

FEES = {"spot_taker": 0.001, "futures_taker": 0.0005, "slippage_bps": 0}


class FakeClient:
    """Mutable last price, no minimums."""
    def __init__(self, price=100.0):
        self.price = price

    def fetch_ticker(self, symbol):
        return {"last": self.price}

    def get_min_order_amount(self, symbol, venue="spot"):
        return 0.0


@pytest.fixture
def om(monkeypatch):
    m = OrderManager(FakeClient(), paper_mode=True, paper_balance=10_000.0,
                     fees_cfg=dict(FEES))
    ids = iter(range(1, 100))
    monkeypatch.setattr(m, "_log_trade", lambda *a, **k: next(ids))
    monkeypatch.setattr(m, "_close_trade", lambda *a, **k: True)
    monkeypatch.setattr(m, "_partial_close", lambda *a, **k: True)
    return m


class TestLongRoundTrip:
    def test_open_deducts_cost_plus_fee(self, om):
        r = om.open_position("BTC/USDT", "buy", 1000.0, "test", 95, 110)
        assert r is not None
        # cost 1000 + fee (1000 * 0.001) = 1001
        assert om.free_cash() == pytest.approx(10_000 - 1001.0)

    def test_profitable_close_nets_fees(self, om):
        r = om.open_position("BTC/USDT", "buy", 1000.0, "test", 95, 110)
        om.client.price = 110.0
        om.close_position("BTC/USDT", r["quantity"], r["trade_id"], side="buy")
        # gross +100, entry fee 1.0, exit fee 110*10*0.001 = 1.1 → net +97.9
        assert om.free_cash() == pytest.approx(10_000 + 97.9)
        assert om._paper_positions == {}

    def test_losing_close_also_pays_fees(self, om):
        r = om.open_position("BTC/USDT", "buy", 1000.0, "test", 95, 110)
        om.client.price = 95.0
        om.close_position("BTC/USDT", r["quantity"], r["trade_id"], side="buy")
        # gross -50, entry fee 1.0, exit fee 0.95 → net -51.95
        assert om.free_cash() == pytest.approx(10_000 - 51.95)


class TestShortRoundTrip:
    def test_short_profits_when_price_falls(self, om):
        r = om.open_position("BTC/USDT", "sell", 1000.0, "test", 103, 90)
        om.client.price = 90.0
        om.close_position("BTC/USDT", r["quantity"], r["trade_id"], side="sell")
        # gross +100; futures fees: entry 0.5, exit 90*10*0.0005 = 0.45
        assert om.free_cash() == pytest.approx(10_000 + 99.05)

    def test_short_loses_when_price_rises(self, om):
        r = om.open_position("BTC/USDT", "sell", 1000.0, "test", 103, 90)
        om.client.price = 105.0
        om.close_position("BTC/USDT", r["quantity"], r["trade_id"], side="sell")
        # gross -50; fees 0.5 + 105*10*0.0005 = 1.025 → net -51.025
        assert om.free_cash() == pytest.approx(10_000 - 51.025)

    def test_short_uses_futures_fee_rate(self, om):
        assert om.fee_rate("sell") == 0.0005
        assert om.fee_rate("buy") == 0.001


class TestSlippage:
    def test_buy_fills_above_sell_fills_below(self, monkeypatch):
        m = OrderManager(FakeClient(), paper_mode=True, paper_balance=10_000.0,
                         fees_cfg={**FEES, "slippage_bps": 10})  # 0.1%
        monkeypatch.setattr(m, "_log_trade", lambda *a, **k: 1)
        r = m.open_position("BTC/USDT", "buy", 1000.0, "t", 95, 110)
        assert r["price"] == pytest.approx(100.1)   # long entry pays up
        r2 = m._slip(100.0, "sell")
        assert r2 == pytest.approx(99.9)            # sells fill low

    def test_slippage_hurts_both_sides_of_round_trip(self, monkeypatch):
        m = OrderManager(FakeClient(), paper_mode=True, paper_balance=10_000.0,
                         fees_cfg={"spot_taker": 0.0, "futures_taker": 0.0,
                                   "slippage_bps": 10})
        monkeypatch.setattr(m, "_log_trade", lambda *a, **k: 1)
        monkeypatch.setattr(m, "_close_trade", lambda *a, **k: True)
        r = m.open_position("BTC/USDT", "buy", 1000.0, "t", 95, 110)
        m.close_position("BTC/USDT", r["quantity"], 1, side="buy")
        # flat price, but two slips of 0.1% → wallet strictly below start
        assert m.free_cash() < 10_000


class TestEquityMarkToMarket:
    def test_long_equity_rises_with_price(self, om):
        om.open_position("BTC/USDT", "buy", 1000.0, "t", 95, 110)
        om.client.price = 105.0
        # cash(8999) + cost(1000) + unreal(+50) = 10049
        assert om.equity() == pytest.approx(10_049.0)

    def test_short_equity_rises_when_price_falls(self, om):
        om.open_position("BTC/USDT", "sell", 1000.0, "t", 103, 90)
        om.client.price = 95.0
        # cash(8999.5) + cost(1000) + unreal(+50) = 10049.5
        assert om.equity() == pytest.approx(10_049.5)


class TestPartialClose:
    def test_partial_releases_half_margin_plus_gross(self, om):
        r = om.open_position("BTC/USDT", "buy", 1000.0, "t", 95, 110)
        om.client.price = 108.0
        cash_before = om.free_cash()
        om.close_position("BTC/USDT", r["quantity"], r["trade_id"],
                          side="buy", fraction=0.5)
        # released margin 500 + gross (8*5=40) - exit fee (108*5*0.001=0.54)
        assert om.free_cash() == pytest.approx(cash_before + 539.46)
        # remainder still open with half the quantity
        pos = om._paper_positions[r["trade_id"]]
        assert pos["qty"] == pytest.approx(5.0)
