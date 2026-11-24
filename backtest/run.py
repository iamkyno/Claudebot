"""
Replay M1 history through the same SMC signals and risk rules the bot uses.

    python -m backtest.run --symbol XAUUSD --csv XAUUSD_M1.csv
    python -m backtest.run --symbol XAUUSD --api --days 30      (pulls M1 from the Exness API)

CSV: an MT5 export (Tools > History Center / Symbols > Bars > Export, tab-separated
<DATE> <TIME> <OPEN> ...), or any CSV with time,open,high,low,close columns.
Bars are bid prices; the spread is added on the ask side. Results are in R
(multiples of the stop distance), so they don't depend on lot size.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from config.settings import load_config
from risk.guard import in_session, sast_day_start
from signals.smc import Bars, SmcParams, find_entry, structure_bias


@dataclass
class Trade:
    side: str
    entry_time: datetime
    entry: float
    sl: float
    tp: float
    exit_time: datetime | None = None
    exit: float | None = None
    outcome: str = ""

    @property
    def r(self) -> float:
        risk = abs(self.entry - self.sl)
        move = self.exit - self.entry if self.side == "buy" else self.entry - self.exit
        return move / risk


def load_csv(path: str, server_utc_offset_hours: float = 0) -> Bars:
    off = timedelta(hours=server_utc_offset_hours)
    t, o, h, l, c = [], [], [], [], []
    with open(path, encoding="utf-8-sig") as f:
        first = f.readline()
        f.seek(0)
        if first.startswith("<DATE>"):
            rows = csv.DictReader(f, delimiter="\t")
            for r in rows:
                ts = datetime.strptime(f"{r['<DATE>']} {r['<TIME>']}", "%Y.%m.%d %H:%M:%S")
                t.append(ts.replace(tzinfo=timezone.utc) - off)
                o.append(float(r["<OPEN>"])); h.append(float(r["<HIGH>"]))
                l.append(float(r["<LOW>"])); c.append(float(r["<CLOSE>"]))
        else:
            for r in csv.DictReader(f):
                raw = r["time"]
                ts = (datetime.fromtimestamp(int(raw), timezone.utc) if raw.isdigit()
                      else datetime.fromisoformat(raw))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                t.append(ts - off)
                o.append(float(r["open"])); h.append(float(r["high"]))
                l.append(float(r["low"])); c.append(float(r["close"]))
    return Bars(t, o, h, l, c)


def to_m5(m1: Bars) -> tuple[Bars, list[int]]:
    """M5 bars plus, for each, the index of its last M1 bar."""
    t, o, h, l, c, last = [], [], [], [], [], []
    for i, ts in enumerate(m1.time):
        bucket = ts.replace(minute=ts.minute - ts.minute % 5, second=0, microsecond=0)
        if t and t[-1] == bucket:
            h[-1] = max(h[-1], m1.high[i]); l[-1] = min(l[-1], m1.low[i])
            c[-1] = m1.close[i]; last[-1] = i
        else:
            t.append(bucket); o.append(m1.open[i]); h.append(m1.high[i])
            l.append(m1.low[i]); c.append(m1.close[i]); last.append(i)
    return Bars(t, o, h, l, c), last


def simulate(m1: Bars, params: SmcParams, spread: float, sessions, risk_cfg: dict,
             entry_bars: int = 150, trend_bars: int = 300) -> list[Trade]:
    m5, m5_last = to_m5(m1)
    max_hold = int(risk_cfg.get("max_hold_minutes", 30))
    max_spread_to_sl = risk_cfg.get("max_spread_to_sl", 0.25)
    max_trades = risk_cfg.get("max_trades_per_day", 6)

    trades: list[Trade] = []
    pos: Trade | None = None
    pos_idx = 0
    pending = None
    seen: set[str] = set()
    per_day: dict[str, int] = {}
    m5_done, bias, bias_at = 0, None, -1

    for t in range(entry_bars, len(m1)):
        if pending is not None:  # fill at this bar's open
            buy = pending.side == "buy"
            fill = m1.open[t] + (spread if buy else 0.0)
            sig = pending.repriced(fill, params.reward_risk)
            pending = None
            if (buy and sig.sl < fill) or (not buy and sig.sl > fill):
                pos, pos_idx = Trade(sig.side, m1.time[t], fill, sig.sl, sig.tp), t

        if pos is not None:
            buy = pos.side == "buy"
            hi = m1.high[t] + (0 if buy else spread)  # shorts exit at the ask
            lo = m1.low[t] + (0 if buy else spread)
            hit_sl = lo <= pos.sl if buy else hi >= pos.sl
            hit_tp = hi >= pos.tp if buy else lo <= pos.tp
            if hit_sl or hit_tp:  # both in one bar -> assume the stop (conservative)
                pos.exit, pos.outcome = (pos.sl, "sl") if hit_sl else (pos.tp, "tp")
            elif t - pos_idx + 1 >= max_hold:
                pos.exit = m1.close[t] + (0 if buy else spread)
                pos.outcome = "time"
            if pos.exit is not None:
                pos.exit_time = m1.time[t]
                trades.append(pos)
                pos = None

        while m5_done < len(m5_last) and m5_last[m5_done] <= t:
            m5_done += 1
        if m5_done != bias_at:
            bias_at = m5_done
            bias = structure_bias(m5.slice(max(0, m5_done - trend_bars), m5_done),
                                  params.trend_swing_length)

        if pos is not None or pending is not None:
            continue
        close_time = m1.time[t] + timedelta(minutes=1)
        if not in_session(close_time, sessions):
            continue
        sig = find_entry(m1.slice(t - entry_bars + 1, t + 1), bias, params)
        if sig is None or sig.setup_key in seen:
            continue
        seen.add(sig.setup_key)
        day, _ = sast_day_start(close_time)
        if per_day.get(day, 0) >= max_trades or spread > max_spread_to_sl * sig.risk:
            continue
        per_day[day] = per_day.get(day, 0) + 1
        pending = sig
    return trades


def report(trades: list[Trade]) -> str:
    if not trades:
        return "No trades."
    rs = [tr.r for tr in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    equity = peak = dd = 0.0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        dd = min(dd, equity - peak)
    pf = sum(wins) / -sum(losses) if losses and sum(losses) < 0 else float("inf")
    by = {k: sum(1 for tr in trades if tr.outcome == k) for k in ("tp", "sl", "time")}
    return (f"Trades {len(rs)} | win rate {len(wins) / len(rs):.1%} | "
            f"net {sum(rs):+.2f}R | avg {sum(rs) / len(rs):+.3f}R | PF {pf:.2f} | "
            f"max DD {dd:.2f}R | exits TP {by['tp']} SL {by['sl']} time {by['time']}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--symbol", required=True, help="base name from config, e.g. XAUUSD")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv")
    src.add_argument("--api", action="store_true")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--utc-offset", type=float, help="hours the CSV times are ahead of UTC")
    ap.add_argument("--spread", type=float, help="price units; default from config")
    ap.add_argument("--trades-csv", help="write every simulated trade here")
    args = ap.parse_args(argv)

    cfg = load_config()
    offset = 0  # CSV times are UTC; MT5 exports may need --utc-offset
    if args.csv:
        m1 = load_csv(args.csv, args.utc_offset if args.utc_offset is not None else offset)
    else:
        from config.settings import exness_credentials
        from exness.api import ExnessApi
        client = ExnessApi(**exness_credentials())  # read-only use
        client.connect()
        end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        m1 = client.history(client.resolve_symbol(args.symbol), "M1",
                            end - timedelta(days=args.days), end)

    sym_cfg = (cfg.get("symbols") or {}).get(args.symbol, {}) or {}
    spread = args.spread if args.spread is not None else \
        cfg.get("backtest", {}).get("spread", {}).get(args.symbol, 0.0)
    strat = cfg.get("strategy", {})
    trades = simulate(m1, SmcParams.from_cfg(strat), spread, sym_cfg.get("sessions", "always"),
                      cfg.get("risk", {}), strat.get("entry_bars", 150), strat.get("trend_bars", 300))
    print(f"{args.symbol}: {len(m1)} M1 bars, spread {spread}")
    print(report(trades))
    if args.trades_csv:
        with open(args.trades_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["side", "entry_time", "entry", "sl", "tp", "exit_time", "exit", "outcome", "r"])
            for tr in trades:
                w.writerow([tr.side, tr.entry_time.isoformat(), tr.entry, tr.sl, tr.tp,
                            tr.exit_time.isoformat(), tr.exit, tr.outcome, round(tr.r, 3)])


if __name__ == "__main__":
    main()
