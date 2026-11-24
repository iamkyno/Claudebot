from types import SimpleNamespace

import pytest

from exness.mt5_client import Mt5Client, Mt5Error


class FakeMt5:
    ACCOUNT_TRADE_MODE_DEMO, ACCOUNT_TRADE_MODE_REAL = 0, 2
    TIMEFRAME_M1, TIMEFRAME_M5 = 1, 5
    TRADE_ACTION_DEAL, ORDER_TYPE_BUY, ORDER_TYPE_SELL = 1, 0, 1
    ORDER_TIME_GTC, ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 0, 1, 2
    TRADE_RETCODE_DONE, POSITION_TYPE_BUY = 10009, 0
    DEAL_ENTRY_IN, DEAL_ENTRY_OUT, DEAL_ENTRY_OUT_BY = 0, 1, 3

    def __init__(self):
        self.sent = []
        self.info = SimpleNamespace(digits=2, filling_mode=2, point=0.01, volume_min=0.01,
                                    volume_max=200, volume_step=0.01, trade_tick_size=0.01,
                                    trade_tick_value=1.0, trade_stops_level=0)

    def symbol_info(self, name):
        return self.info if name == "XAUUSDm" else None

    def symbols_get(self, group):
        return [SimpleNamespace(name="XAUUSDm"), SimpleNamespace(name="XAUUSDc_old")]

    def symbol_select(self, name, enable):
        return True

    def symbol_info_tick(self, name):
        return SimpleNamespace(bid=2000.0, ask=2000.2, time=1_700_000_000)

    def order_send(self, req):
        self.sent.append(req)
        return SimpleNamespace(retcode=10009, order=555, price=req["price"],
                               volume=req["volume"], comment="done")

    def history_deals_get(self, position):
        return [SimpleNamespace(entry=0, profit=0.0, commission=-0.5, swap=0.0, price=2000.2,
                                time=1_700_000_000),
                SimpleNamespace(entry=1, profit=10.0, commission=-0.5, swap=-0.2, price=2010.0,
                                time=1_700_000_600)]

    def account_info(self):
        return SimpleNamespace(trade_mode=2)


def test_resolves_exness_suffix():
    assert Mt5Client(module=FakeMt5()).resolve_symbol("XAUUSD") == "XAUUSDm"


def test_unknown_symbol_raises():
    m = FakeMt5()
    m.symbols_get = lambda group: []
    with pytest.raises(Mt5Error):
        Mt5Client(module=m).resolve_symbol("EURUSD")


def test_market_order_request():
    m = FakeMt5()
    res = Mt5Client(module=m, magic=42).market_order("XAUUSDm", "buy", 0.05, 1995.123, 2009.987)
    req = m.sent[0]
    assert res.ok and res.ticket == 555
    assert req["type"] == m.ORDER_TYPE_BUY and req["price"] == 2000.2
    assert req["sl"] == 1995.12 and req["tp"] == 2009.99
    assert req["magic"] == 42 and req["type_filling"] == m.ORDER_FILLING_IOC


def test_closed_result_nets_costs():
    r = Mt5Client(module=FakeMt5()).closed_result(555)
    assert r.exit_price == 2010.0 and abs(r.profit - 8.8) < 1e-9


def test_real_account_is_not_demo():
    assert Mt5Client(module=FakeMt5()).is_demo() is False
