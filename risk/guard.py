"""Checks run before every order, plus lot sizing. No I/O except the kill file."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

SAST = timezone(timedelta(hours=2), "SAST")
_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


@dataclass(frozen=True)
class SymbolSpec:
    volume_min: float
    volume_max: float
    volume_step: float
    tick_size: float
    tick_value: float   # account currency per tick per 1.0 lot
    point: float
    stops_level: int = 0  # broker's minimum stop distance, in points


@dataclass(frozen=True)
class Context:
    now_utc: datetime
    bid: float
    ask: float
    equity: float
    day_start_equity: float
    trades_today: int
    open_positions: int


@dataclass(frozen=True)
class Decision:
    ok: bool
    reason: str = ""


def sast_day_start(now_utc: datetime) -> tuple[str, datetime]:
    """SAST calendar day key and its midnight in UTC."""
    local = now_utc.astimezone(SAST)
    midnight = datetime.combine(local.date(), time(0), SAST)
    return local.date().isoformat(), midnight.astimezone(timezone.utc)


def in_session(now_utc: datetime, sessions) -> bool:
    if sessions in (None, "always", "24/7"):
        return True
    local = now_utc.astimezone(SAST)
    hm = local.time().replace(second=0, microsecond=0)
    for w in sessions:
        days = {_DAYS[d.lower()[:3]] for d in w.get("days", _DAYS)}
        start = time.fromisoformat(w["start"])
        end = time.fromisoformat(w["end"])
        if start <= end:
            if local.weekday() in days and start <= hm < end:
                return True
        else:  # window crosses midnight; the start day owns it
            if (local.weekday() in days and hm >= start) or \
               ((local.weekday() - 1) % 7 in days and hm < end):
                return True
    return False


def lot_size(equity: float, risk_pct: float, entry: float, sl: float, spec: SymbolSpec,
             max_lots: float | None = None) -> float:
    """Lots so a stop-out loses risk_pct of equity. Rounds DOWN; 0 if below the broker minimum.
    max_lots caps it further (e.g. 0.01 while testing)."""
    dist = abs(entry - sl)
    if dist <= 0 or spec.tick_size <= 0 or spec.tick_value <= 0 or spec.volume_step <= 0:
        return 0.0
    loss_per_lot = dist / spec.tick_size * spec.tick_value
    raw = equity * risk_pct / 100.0 / loss_per_lot
    steps = math.floor(raw / spec.volume_step + 1e-9)
    decimals = max(0, -int(math.floor(math.log10(spec.volume_step))))
    cap = min(spec.volume_max, max_lots) if max_lots else spec.volume_max
    lots = round(min(steps * spec.volume_step, cap), decimals)
    return lots if lots >= spec.volume_min else 0.0


class RiskGuard:
    def __init__(self, cfg: dict, root: Path | None = None):
        self.max_trades = cfg.get("max_trades_per_day", 6)
        self.max_daily_loss_pct = cfg.get("max_daily_loss_pct", 2.0)
        self.max_spread_to_sl = cfg.get("max_spread_to_sl", 0.25)
        self.max_open = cfg.get("max_open_per_symbol", 1)
        self.kill_file = (root or Path.cwd()) / cfg.get("kill_file", "KILL")

    def daily_loss_hit(self, ctx: Context) -> bool:
        if ctx.day_start_equity <= 0:
            return False
        dd = (ctx.equity - ctx.day_start_equity) / ctx.day_start_equity * 100
        return dd <= -self.max_daily_loss_pct

    def evaluate(self, signal, ctx: Context, sessions, spec: SymbolSpec) -> Decision:
        if self.kill_file.exists():
            return Decision(False, f"kill switch file {self.kill_file.name} present")
        if self.daily_loss_hit(ctx):
            return Decision(False, f"daily loss limit {self.max_daily_loss_pct}% reached")
        if ctx.trades_today >= self.max_trades:
            return Decision(False, f"{self.max_trades} trades already today")
        if not in_session(ctx.now_utc, sessions):
            return Decision(False, "outside trading hours")
        if ctx.open_positions >= self.max_open:
            return Decision(False, "position already open on this symbol")
        spread = ctx.ask - ctx.bid
        if spread > self.max_spread_to_sl * signal.risk:
            return Decision(False, f"spread {spread:.5g} too wide for a {signal.risk:.5g} stop")
        if spec.stops_level and signal.risk < spec.stops_level * spec.point:
            return Decision(False, "stop closer than the broker allows")
        if (signal.side == "buy" and signal.sl >= signal.entry) or \
           (signal.side == "sell" and signal.sl <= signal.entry):
            return Decision(False, "price already through the stop")
        return Decision(True)
