"""Thin wrapper around the MetaTrader5 package for an Exness MT5 terminal."""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from risk.guard import SymbolSpec
from signals.smc import Bars

log = logging.getLogger(__name__)

_TIMEFRAMES = {"M1": "TIMEFRAME_M1", "M5": "TIMEFRAME_M5", "M15": "TIMEFRAME_M15",
               "H1": "TIMEFRAME_H1"}


class Mt5Error(RuntimeError):
    pass


def _load_mt5(bridge: dict):
    if sys.platform == "win32":
        try:
            import MetaTrader5  # noqa: N813
            return MetaTrader5
        except ImportError as e:
            raise Mt5Error("MetaTrader5 package not installed: pip install MetaTrader5") from e
    from exness.bridge_client import BridgeError, RemoteMt5
    try:
        return RemoteMt5(**bridge)
    except BridgeError as e:
        raise Mt5Error(str(e)) from e


@dataclass(frozen=True)
class OrderResult:
    ok: bool
    ticket: int | None
    price: float
    volume: float
    message: str = ""


@dataclass(frozen=True)
class ClosedTrade:
    exit_time: datetime
    exit_price: float
    profit: float  # net of commission and swap, account currency


class Mt5Client:
    def __init__(self, server_utc_offset_hours: float = 0, magic: int = 7140001,
                 deviation_points: int = 30, module=None, bridge: dict | None = None):
        """module: a MetaTrader5-like object (tests). Otherwise the real package on
        Windows, or the Wine bridge (bridge = {host, port, key}) everywhere else."""
        if module is None:
            module = _load_mt5(bridge or {})
        self.mt5 = module
        self.offset = timedelta(hours=server_utc_offset_hours)
        self.magic = magic
        self.deviation = deviation_points

    # --- session -----------------------------------------------------------------
    def connect(self, login=None, password="", server="", path=""):
        kwargs = {}
        if path:
            kwargs["path"] = path
        if login:
            kwargs.update(login=int(login), password=password, server=server)
        if not self.mt5.initialize(**kwargs):
            raise Mt5Error(f"MT5 initialize failed: {self.mt5.last_error()}. "
                           "Is the MT5 terminal installed, open and logged in?")

    def shutdown(self):
        self.mt5.shutdown()

    def account(self):
        info = self.mt5.account_info()
        if info is None:
            raise Mt5Error(f"No account info: {self.mt5.last_error()}")
        return info

    def is_demo(self, info=None) -> bool:
        info = info or self.account()
        return info.trade_mode == self.mt5.ACCOUNT_TRADE_MODE_DEMO

    # --- market data -------------------------------------------------------------
    def resolve_symbol(self, base: str) -> str:
        """XAUUSD -> XAUUSDm etc. Prefers the exact name, else the shortest suffixed match."""
        name = base if self.mt5.symbol_info(base) is not None else None
        if name is None:
            cands = self.mt5.symbols_get(group=f"*{base}*") or []
            names = sorted((s.name for s in cands if s.name.upper().startswith(base.upper())),
                           key=len)
            if not names:
                raise Mt5Error(f"Symbol {base} not found on this account")
            name = names[0]
        if not self.mt5.symbol_select(name, True):
            raise Mt5Error(f"Could not add {name} to Market Watch")
        return name

    def spec(self, symbol: str) -> SymbolSpec:
        i = self.mt5.symbol_info(symbol)
        if i is None:
            raise Mt5Error(f"No symbol info for {symbol}")
        return SymbolSpec(i.volume_min, i.volume_max, i.volume_step, i.trade_tick_size,
                          i.trade_tick_value, i.point, i.trade_stops_level)

    def tick(self, symbol: str) -> tuple[float, float, datetime]:
        t = self.mt5.symbol_info_tick(symbol)
        if t is None:
            raise Mt5Error(f"No tick for {symbol}")
        return t.bid, t.ask, self._utc(t.time)

    def rates(self, symbol: str, timeframe: str, count: int) -> Bars:
        """Last `count` CLOSED bars (position 1 skips the bar still forming)."""
        tf = getattr(self.mt5, _TIMEFRAMES[timeframe])
        return self._bars(self.mt5.copy_rates_from_pos(symbol, tf, 1, count))

    def history(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> Bars:
        tf = getattr(self.mt5, _TIMEFRAMES[timeframe])
        return self._bars(self.mt5.copy_rates_range(symbol, tf, start + self.offset,
                                                    end + self.offset))

    def _bars(self, r) -> Bars:
        if r is None or len(r) == 0:
            return Bars([], [], [], [], [])
        return Bars([self._utc(x["time"]) for x in r], [float(x["open"]) for x in r],
                    [float(x["high"]) for x in r], [float(x["low"]) for x in r],
                    [float(x["close"]) for x in r])

    def _utc(self, server_seconds) -> datetime:
        return datetime.fromtimestamp(int(server_seconds), timezone.utc) - self.offset

    # --- trading -----------------------------------------------------------------
    def positions(self, symbol: str | None = None) -> list:
        ps = self.mt5.positions_get(symbol=symbol) if symbol else self.mt5.positions_get()
        return [p for p in (ps or []) if p.magic == self.magic]

    def _filling(self, symbol: str) -> int:
        mode = self.mt5.symbol_info(symbol).filling_mode
        if mode & 1:
            return self.mt5.ORDER_FILLING_FOK
        if mode & 2:
            return self.mt5.ORDER_FILLING_IOC
        return self.mt5.ORDER_FILLING_RETURN

    def _send(self, request: dict) -> OrderResult:
        res = self.mt5.order_send(request)
        if res is None:
            return OrderResult(False, None, 0.0, 0.0, f"order_send failed: {self.mt5.last_error()}")
        ok = res.retcode == self.mt5.TRADE_RETCODE_DONE
        return OrderResult(ok, res.order if ok else None, res.price, res.volume,
                           f"{res.retcode} {res.comment}")

    def market_order(self, symbol: str, side: str, volume: float, sl: float, tp: float,
                     comment: str = "") -> OrderResult:
        bid, ask, _ = self.tick(symbol)
        buy = side == "buy"
        digits = self.mt5.symbol_info(symbol).digits
        return self._send({
            "action": self.mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(volume),
            "type": self.mt5.ORDER_TYPE_BUY if buy else self.mt5.ORDER_TYPE_SELL,
            "price": ask if buy else bid,
            "sl": round(sl, digits),
            "tp": round(tp, digits),
            "deviation": self.deviation,
            "magic": self.magic,
            "comment": comment[:31],
            "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": self._filling(symbol),
        })

    def close_position(self, pos, comment: str = "time stop") -> OrderResult:
        bid, ask, _ = self.tick(pos.symbol)
        closing_buy = pos.type == self.mt5.POSITION_TYPE_BUY
        return self._send({
            "action": self.mt5.TRADE_ACTION_DEAL,
            "symbol": pos.symbol,
            "volume": pos.volume,
            "type": self.mt5.ORDER_TYPE_SELL if closing_buy else self.mt5.ORDER_TYPE_BUY,
            "position": pos.ticket,
            "price": bid if closing_buy else ask,
            "deviation": self.deviation,
            "magic": self.magic,
            "comment": comment[:31],
            "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": self._filling(pos.symbol),
        })

    def closed_result(self, ticket: int) -> ClosedTrade | None:
        deals = self.mt5.history_deals_get(position=ticket) or []
        outs = [d for d in deals if d.entry in (self.mt5.DEAL_ENTRY_OUT, self.mt5.DEAL_ENTRY_OUT_BY)]
        if not outs:
            return None
        last = max(outs, key=lambda d: d.time)
        profit = sum(d.profit + d.commission + d.swap for d in deals)
        return ClosedTrade(self._utc(last.time), last.price, profit)
