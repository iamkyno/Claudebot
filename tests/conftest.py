import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from risk.guard import SymbolSpec  # noqa: E402
from signals.smc import Bars, SmcParams  # noqa: E402

T0 = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)  # Tue 12:00 SAST

# (high, low, close) — swing low 98 @4, swing high 103 @8, sweep 97.6 @12,
# break of 103 @15 leaving FVG 100.5-102.5, retest @18.
LONG_SETUP = [
    (103, 102, 102.5), (102, 101, 101.5), (101, 100, 100.5), (100, 99, 99.5), (99, 98, 98.5),
    (100, 98.5, 99.5), (101, 99.5, 100.5), (102, 100.5, 101.5), (103, 101.5, 102.5),
    (102.5, 101, 101.5), (101.5, 100, 100.5), (100.5, 99, 99.5), (99.5, 97.6, 98.5),
    (100.5, 98.8, 100), (102, 100, 101.5), (104.5, 102.5, 104), (105, 103.5, 104.2),
    (104.5, 103, 103.8), (103.5, 102.2, 103),
]

PARAMS = SmcParams(max_sl_atr=10)


def make_bars(rows, start=T0, minutes=1) -> Bars:
    t = [start + timedelta(minutes=minutes * i) for i in range(len(rows))]
    closes = [r[2] for r in rows]
    opens = [closes[0]] + closes[:-1]
    return Bars(t, opens, [r[0] for r in rows], [r[1] for r in rows], closes)


def uptrend_m5(n=40) -> Bars:
    rows, base = [], 100.0
    for i in range(n):  # zigzag: 3 up, 2 down, rising
        base += 1.0 if i % 5 < 3 else -0.8
        rows.append((base + 0.3, base - 0.3, base))
    return make_bars(rows, T0 - timedelta(hours=4), 5)


SPEC = SymbolSpec(volume_min=0.01, volume_max=100, volume_step=0.01, tick_size=0.01,
                  tick_value=1.0, point=0.01, stops_level=0)


class FakeClient:
    """Stands in for Mt5Client in engine tests."""

    account_id = "123456"

    def __init__(self, m1, m5, bid=102.95, ask=103.0, equity=10_000.0):
        self.m1, self.m5 = m1, m5
        self.bid, self.ask, self.equity = bid, ask, equity
        self.orders, self.open_positions = [], []

    def resolve_symbol(self, base):
        return base + "m"

    def account(self):
        return SimpleNamespace(equity=self.equity, login=self.account_id, currency="USD")

    def rates(self, symbol, tf, count):
        return self.m1 if tf == "M1" else self.m5

    def tick(self, symbol):
        return self.bid, self.ask, T0

    def spec(self, symbol):
        return SPEC

    def positions(self, symbol=None):
        return [p for p in self.open_positions if symbol is None or p.symbol == symbol]

    def market_order(self, symbol, side, volume, sl, tp, comment=""):
        from exness.api import OrderResult
        self.orders.append((symbol, side, volume, sl, tp))
        ticket = 1000 + len(self.orders)
        self.open_positions.append(SimpleNamespace(ticket=ticket, symbol=symbol, type=side,
                                                   volume=volume))
        return OrderResult(True, ticket, self.ask, volume, "10009 done")

    def close_position(self, pos):
        from exness.api import OrderResult
        self.open_positions.remove(pos)
        return OrderResult(True, pos.ticket, self.bid, pos.volume, "closed")

    def closed_result(self, ticket, since):
        from exness.api import ClosedTrade
        return ClosedTrade(T0 + timedelta(minutes=5), 104.0, 12.5)
