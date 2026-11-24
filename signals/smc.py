"""
Smart-money-concepts signals, pure Python so the live bot and the backtest
run the exact same logic.

Bias (M5): direction of the most recent break of a confirmed swing point.
Entry (M1), long side; shorts are computed by mirroring prices:
    swing low L  ->  swing high H  ->  sweep below L  ->  close above H (CHoCH)
    ->  bullish FVG left inside that move  ->  first retrace into the FVG = entry.
Stop goes under the sweep wick, target is reward_risk x the stop distance.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime


@dataclass(frozen=True)
class Bars:
    """Closed candles, oldest first. Times are bar-open times in UTC."""

    time: list[datetime]
    open: list[float]
    high: list[float]
    low: list[float]
    close: list[float]

    def __len__(self) -> int:
        return len(self.close)

    def slice(self, start: int, stop: int) -> "Bars":
        return Bars(self.time[start:stop], self.open[start:stop], self.high[start:stop],
                    self.low[start:stop], self.close[start:stop])

    def mirrored(self) -> "Bars":
        """Price-negated copy: a short setup becomes a long one."""
        return Bars(list(self.time), [-x for x in self.open], [-x for x in self.low],
                    [-x for x in self.high], [-x for x in self.close])


@dataclass(frozen=True)
class SmcParams:
    trend_swing_length: int = 3
    entry_swing_length: int = 2
    max_setup_age_bars: int = 15
    sweep_max_atr: float = 1.5
    fvg_min_atr: float = 0.05
    sl_buffer_atr: float = 0.1
    min_sl_atr: float = 0.5
    max_sl_atr: float = 4.0
    reward_risk: float = 1.8
    atr_period: int = 14

    @classmethod
    def from_cfg(cls, cfg: dict) -> "SmcParams":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (cfg or {}).items() if k in names})


@dataclass(frozen=True)
class Signal:
    side: str            # "buy" | "sell"
    entry: float         # close of the trigger bar (repriced to bid/ask before sending)
    sl: float
    tp: float
    swept: float         # liquidity level that was taken
    broken: float        # structure level that was broken
    fvg: tuple[float, float]  # (bottom, top)
    setup_key: str       # unique per setup, so one setup trades once

    def mirrored(self) -> "Signal":
        return Signal(self.side, -self.entry, -self.sl, -self.tp, -self.swept, -self.broken,
                      (-self.fvg[1], -self.fvg[0]), self.setup_key)

    def repriced(self, entry: float, reward_risk: float) -> "Signal":
        """Keep the stop, move the entry to the real fill price, rebuild the target."""
        risk = entry - self.sl if self.side == "buy" else self.sl - entry
        tp = entry + reward_risk * risk if self.side == "buy" else entry - reward_risk * risk
        return Signal(self.side, entry, self.sl, tp, self.swept, self.broken, self.fvg, self.setup_key)

    @property
    def risk(self) -> float:
        return abs(self.entry - self.sl)

    @property
    def reason(self) -> str:
        return (f"swept {self.swept:.5g}, broke {self.broken:.5g}, "
                f"retest FVG {self.fvg[0]:.5g}-{self.fvg[1]:.5g}")


@dataclass(frozen=True)
class Swing:
    idx: int
    price: float
    kind: str  # "high" | "low"


def atr(b: Bars, period: int = 14) -> float:
    n = len(b)
    if n < 2:
        return 0.0
    start = max(1, n - period)
    trs = [max(b.high[i], b.close[i - 1]) - min(b.low[i], b.close[i - 1]) for i in range(start, n)]
    return sum(trs) / len(trs)


def find_swings(b: Bars, length: int) -> list[Swing]:
    """Fractal swing points; a swing at idx is only known at idx + length."""
    out = []
    for i in range(length, len(b) - length):
        left_h, right_h = b.high[i - length:i], b.high[i + 1:i + length + 1]
        if b.high[i] > max(left_h) and b.high[i] >= max(right_h):
            out.append(Swing(i, b.high[i], "high"))
        left_l, right_l = b.low[i - length:i], b.low[i + 1:i + length + 1]
        if b.low[i] < min(left_l) and b.low[i] <= min(right_l):
            out.append(Swing(i, b.low[i], "low"))
    return out


def structure_bias(b: Bars, length: int) -> str | None:
    """'up' / 'down' from the last close through a confirmed swing, else None."""
    swings = sorted(find_swings(b, length), key=lambda s: s.idx)
    k, hi, lo, bias = 0, None, None, None
    for i in range(len(b)):
        while k < len(swings) and swings[k].idx + length <= i:
            s = swings[k]
            k += 1
            if s.kind == "high":
                hi = s
            else:
                lo = s
        if hi is not None and b.close[i] > hi.price:
            bias, hi = "up", None
        if lo is not None and b.close[i] < lo.price:
            bias, lo = "down", None
    return bias


def find_entry(m1: Bars, bias: str | None, p: SmcParams) -> Signal | None:
    """Signal on the LAST bar of m1 (call once per newly closed M1 bar)."""
    if bias == "up":
        return _long_setup(m1, p, "buy")
    if bias == "down":
        s = _long_setup(m1.mirrored(), p, "sell")
        return s.mirrored() if s else None
    return None


def _long_setup(b: Bars, p: SmcParams, side: str) -> Signal | None:
    n, L = len(b), p.entry_swing_length
    if n < max(p.atr_period + 1, 4 * L + 5):
        return None
    a = atr(b, p.atr_period)
    if a <= 0:
        return None
    swings = find_swings(b, L)
    highs = [s for s in swings if s.kind == "high"]
    t = n - 1

    for brk in range(t - 1, max(t - p.max_setup_age_bars, L + 1) - 1, -1):
        confirmed = [s for s in highs if s.idx + L <= brk]
        if not confirmed:
            continue
        H = confirmed[-1]
        if not (b.close[brk] > H.price >= b.close[brk - 1]):
            continue
        if any(b.close[j] > H.price for j in range(H.idx + 1, brk)):
            continue  # not the first break of this high
        return _evaluate(b, p, side, swings, H, brk, t, a)
    return None


def _evaluate(b: Bars, p: SmcParams, side: str, swings: list[Swing], H: Swing,
              brk: int, t: int, a: float) -> Signal | None:
    lows = [s for s in swings if s.kind == "low" and s.idx < H.idx]
    if not lows:
        return None
    liquidity = lows[-1]

    sweep_idx = min(range(H.idx + 1, brk + 1), key=lambda i: b.low[i])
    sweep_low = b.low[sweep_idx]
    if not sweep_low < liquidity.price:
        return None
    if liquidity.price - sweep_low > p.sweep_max_atr * a:
        return None

    zone = None
    for i in range(brk, sweep_idx + 1, -1):  # newest gap in the displacement leg
        gap = b.low[i] - b.high[i - 2]
        if gap > 0 and gap >= p.fvg_min_atr * a:
            zone = (b.high[i - 2], b.low[i])
            break
    if zone is None:
        return None
    bottom, top = zone

    if any(b.low[j] <= top for j in range(brk + 1, t)):
        return None  # already retested: this setup is spent
    if not (b.low[t] <= top and b.close[t] >= bottom and b.low[t] > sweep_low):
        return None

    entry = b.close[t]
    sl = min(sweep_low - p.sl_buffer_atr * a, entry - p.min_sl_atr * a)
    risk = entry - sl
    if risk <= 0 or risk > p.max_sl_atr * a:
        return None
    return Signal(side, entry, sl, entry + p.reward_risk * risk, liquidity.price, H.price,
                  zone, f"{side}:{b.time[brk].isoformat()}")
