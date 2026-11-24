import threading
from collections import namedtuple
from multiprocessing.connection import Listener
from types import SimpleNamespace

import pytest

from bridge.mt5_server import handle, plain, serve_forever
from exness.bridge_client import BridgeError, RemoteMt5
from exness.mt5_client import Mt5Client

Tick = namedtuple("Tick", "bid ask time")
Symbol = namedtuple("Symbol", "name")
Result = namedtuple("Result", "retcode order price volume comment request")
Req = namedtuple("Req", "symbol volume")

KEY = "test-key"


class FakeModule:
    ACCOUNT_TRADE_MODE_DEMO = 0
    __version__ = "fake"

    def symbol_info_tick(self, name):
        return Tick(2000.0, 2000.2, 1_700_000_000)

    def symbols_get(self, group):
        return (Symbol("XAUUSDm"), Symbol("XAUUSD.raw"))

    def order_send(self, req):
        return Result(10009, 7, req["price"], req["volume"], "done", Req(req["symbol"], 0.1))

    def boom(self):
        raise ValueError("nope")


@pytest.fixture()
def bridge():
    listener = Listener(("127.0.0.1", 0), authkey=KEY.encode())
    threading.Thread(target=serve_forever, args=(listener, FakeModule()), daemon=True).start()
    yield listener.address[1]
    listener.close()


def test_round_trip(bridge):
    mt5 = RemoteMt5("127.0.0.1", bridge, KEY)
    assert mt5.ACCOUNT_TRADE_MODE_DEMO == 0
    t = mt5.symbol_info_tick("XAUUSDm")
    assert (t.bid, t.ask) == (2000.0, 2000.2)
    assert [s.name for s in mt5.symbols_get(group="*XAUUSD*")] == ["XAUUSDm", "XAUUSD.raw"]
    r = mt5.order_send({"symbol": "XAUUSDm", "price": 2000.2, "volume": 0.1})
    assert r.retcode == 10009 and r.request.symbol == "XAUUSDm"


def test_errors_come_back_as_exceptions(bridge):
    mt5 = RemoteMt5("127.0.0.1", bridge, KEY)
    with pytest.raises(BridgeError, match="nope"):
        mt5.boom()
    with pytest.raises(BridgeError):
        mt5.not_a_thing
    assert mt5.symbol_info_tick("x").bid == 2000.0  # still usable afterwards


def test_wrong_key_rejected(bridge):
    with pytest.raises(BridgeError):
        RemoteMt5("127.0.0.1", bridge, "wrong")


def test_bridge_not_running():
    with pytest.raises(BridgeError, match="start_bridge"):
        RemoteMt5("127.0.0.1", 1, KEY)


def test_private_names_refused():
    assert handle(FakeModule(), ("attr", "__class__"))[0] == "err"


def test_mt5client_over_bridge(bridge):
    c = Mt5Client(module=RemoteMt5("127.0.0.1", bridge, KEY))
    bid, ask, _ = c.tick("XAUUSDm")
    assert ask == 2000.2


def test_plain_structured_array():
    np = pytest.importorskip("numpy")
    a = np.array([(1_700_000_000, 1.5, 2.0)], dtype=[("time", "i8"), ("open", "f8"), ("high", "f8")])
    assert plain(a) == [{"time": 1_700_000_000, "open": 1.5, "high": 2.0}]
    assert plain(np.float64(1.25)) == 1.25
