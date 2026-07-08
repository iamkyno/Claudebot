"""
Strategy parameter optimizer — the bot derives its own parameters from
evidence, not from the last handful of live trades.

Two sweeps, each replaying the live entry rules + fee model on real history:

  scalp      75 bracket geometries (min-edge x TP x SL multiples) on 1m data
  ema_cross  36 geometries (stop-ATR x TP-ATR x ADX gate) on 1h data —
             the live star performer, tuned across a year of history

Survivors must be profitable in at least 2 of 3 time folds (walk-forward)
so one lucky regime can't pick the params.

    python -m backtest.optimize                        # both sweeps + report
    python -m backtest.optimize --strategy ema_cross   # one strategy only
    python -m backtest.optimize --apply                # write config/tuning.json

The bot reads config/tuning.json at startup (see config.settings.get_tuning)
— optimizer values override the hand-set defaults per strategy.
"""

import argparse
import itertools
import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_TUNING_PATH = Path(__file__).parent.parent / "config" / "tuning.json"

# Scalp grid: 5 x 5 x 3 = 75 geometries.
GRID = {
    "min_edge_mult": [2.0, 2.5, 3.0, 3.5, 4.0],
    "tp_mult":       [1.0, 1.3, 1.6, 2.0, 2.5],
    "sl_mult":       [0.4, 0.6, 0.8],
}

# Grid-strategy grid: 3 x 4 x 4 x 3 = 144 geometries.
# First sweep (spacing<=0.8, stop<=6, tp<=2, adx>=25): all 81 negative, and
# the least-bad combo sat at the boundary on ALL FOUR dimensions — so the
# space now extends past every one of them (wider spacing, much wider stops,
# bigger targets, stricter trend gates) to see whether the gradient reaches
# profit or just asymptotes below zero.
GRID_GRID = {
    "spacing_mult": [0.5, 0.8, 1.2],
    "stop_mult":    [4.0, 6.0, 9.0, 12.0],
    "tp_mult":      [1.5, 2.0, 3.0, 4.0],
    "adx_kill":     [15, 20, 25],
}

# EMA-cross grid: 4 x 5 x 4 = 80 geometries (defaults: 2.25 / 4.5 / 20).
# First 12-month sweep: the winner sat at the tp=6.0 boundary and every
# top-10 row carried adx_min=25 — so the grid now extends past both
# (tp up to 10, adx up to 30) to find where the curve actually turns.
EMA_GRID = {
    "stop_atr": [1.5, 2.25, 3.0, 4.0],
    "tp_atr":   [3.0, 4.5, 6.0, 8.0, 10.0],
    "adx_min":  [15, 20, 25, 30],
}

TIME_STOP_BARS = 15          # mirrors scalp_time_stop_min on 1m bars
EMA_MAX_HOLD_BARS = 500      # swing safety cap (~3 weeks of 1h bars)
FUTURES_TAKER = 0.0005
FUTURES_MAKER = 0.0002
SPOT_TAKER = 0.001           # ema_cross longs run on spot
SLIP = 0.0002                # 2 bps market-order slippage per side


# ---------------------------------------------------------------------- #
# Feature precompute — ONCE per dataset; combos only change thresholds.

def precompute(df: pd.DataFrame) -> dict:
    """Wilder RSI/ATR, session VWAP, volume ratio, 5m trend gate — the same
    quantities the live scalper reads, vectorized over the whole history."""
    close, high, low, vol = (df[c].astype(float) for c in ("close", "high", "low", "volume"))

    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)

    tr = pd.concat([high - low, (high - close.shift()).abs(),
                    (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean()

    typical = (high + low + close) / 3
    dates = df.index.normalize()
    vwap = ((typical * vol).groupby(dates).cumsum()
            / vol.groupby(dates).cumsum().replace(0, np.nan))

    volr = vol / vol.rolling(20).mean().replace(0, np.nan)
    pc1 = close.pct_change()
    uptick = close > close.shift()

    # 5m trend gate, forward-filled back onto the 1m index.
    c5 = close.resample("5min").last()
    e21 = c5.ewm(span=21, adjust=False).mean()
    e50 = c5.ewm(span=50, adjust=False).mean()
    trend5 = ((e21 >= e50) & (c5 >= e21)).reindex(df.index, method="ffill").fillna(False)

    return {
        "open": df["open"].astype(float).to_numpy(),
        "high": high.to_numpy(), "low": low.to_numpy(), "close": close.to_numpy(),
        "rsi": rsi.to_numpy(), "atr_pct": (atr / close).to_numpy(),
        "vwap_dev": ((vwap - close) / close).to_numpy(),
        "volr": volr.to_numpy(), "pc1": pc1.to_numpy(),
        "uptick": uptick.to_numpy(), "trend": trend5.to_numpy(),
        "n": len(df),
    }


# ---------------------------------------------------------------------- #

def simulate(F: dict, min_edge_mult: float, tp_mult: float, sl_mult: float,
             entry: str = "maker") -> np.ndarray:
    """Run one bracket geometry over precomputed features.
    Returns the array of per-trade net returns (fractions)."""
    rt = FUTURES_TAKER * 2
    edge = np.maximum(rt * min_edge_mult, F["atr_pct"] * 0.8)

    setup_a = (F["vwap_dev"] > edge) & (F["rsi"] < 40) & F["uptick"] & (F["volr"] > 1.0)
    setup_c = (F["volr"] > 2.5) & F["uptick"] & (F["pc1"] > edge)
    candidates = np.flatnonzero(
        (setup_a | setup_c) & F["trend"] & (F["atr_pct"] >= rt)
    )

    fee_in = FUTURES_MAKER if entry == "maker" else FUTURES_TAKER
    slip_in = 0.0 if entry == "maker" else SLIP
    o, h, l, c = F["open"], F["high"], F["low"], F["close"]
    n = F["n"]

    rets, busy_until = [], -1
    for i in candidates:
        if i <= busy_until or i + 1 >= n:
            continue
        e = edge[i]
        entry_px = o[i + 1] * (1 + slip_in)     # fill on the NEXT bar's open
        tp = entry_px * (1 + e * tp_mult)
        sl = entry_px * (1 - e * sl_mult)

        exit_px = None
        last = min(i + 1 + TIME_STOP_BARS, n - 1)
        for j in range(i + 1, last + 1):
            if l[j] <= sl:                       # stop checked first: pessimistic
                exit_px = sl
                break
            if h[j] >= tp:
                exit_px = tp
                break
        if exit_px is None:
            exit_px = c[last]                    # time stop
            j = last

        exit_eff = exit_px * (1 - SLIP)          # market exit crosses the spread
        rets.append((exit_eff - entry_px) / entry_px - fee_in - FUTURES_TAKER)
        busy_until = j

    return np.array(rets)


def score(rets: np.ndarray) -> dict:
    if len(rets) == 0:
        return {"n": 0, "wr": 0.0, "net_bps": 0.0, "pf": 0.0, "avg_bps": 0.0}
    wins, losses = rets[rets > 0], rets[rets <= 0]
    pf = float(wins.sum() / -losses.sum()) if len(losses) and losses.sum() < 0 else float("inf")
    return {
        "n": int(len(rets)),
        "wr": round(float(len(wins) / len(rets) * 100), 1),
        "net_bps": round(float(rets.sum() * 10_000), 1),
        "pf": round(pf, 2),
        "avg_bps": round(float(rets.mean() * 10_000), 2),
    }


def sweep(frames: dict[str, pd.DataFrame], entry: str = "maker") -> list[dict]:
    """Evaluate every geometry over every symbol; walk-forward across 3 time
    folds — a combo must be net-positive in >=2 folds to survive."""
    pre = {}
    for sym, df in frames.items():
        logger.info(f"Precomputing features for {sym} ({len(df):,} bars)")
        pre[sym] = precompute(df)

    results = []
    combos = list(itertools.product(*GRID.values()))
    for k, (me, tp, sl) in enumerate(combos, 1):
        all_rets, fold_nets = [], [0.0, 0.0, 0.0]
        for sym, F in pre.items():
            rets = simulate(F, me, tp, sl, entry)
            all_rets.append(rets)
            # Assign each simulated trade to a time fold by candidate density.
            if len(rets):
                thirds = np.array_split(rets, 3)
                for fi in range(3):
                    fold_nets[fi] += float(thirds[fi].sum()) if len(thirds[fi]) else 0.0
        rets = np.concatenate(all_rets) if all_rets else np.array([])
        s = score(rets)
        s.update({"min_edge_mult": me, "tp_mult": tp, "sl_mult": sl,
                  "folds_positive": sum(1 for f in fold_nets if f > 0)})
        results.append(s)
        if k % 15 == 0:
            logger.info(f"…{k}/{len(combos)} geometries evaluated")

    return results


# ---------------------------------------------------------------------- #
# EMA-cross sweep (1h swing) — mirrors strategies/ema_cross.py:
# volatility-adaptive EMA periods, ADX trend gate, volume confirmation,
# ATR bracket, exit on bearish cross.

def ema_precompute(df: pd.DataFrame) -> dict:
    close, high, low, vol = (df[c].astype(float) for c in ("close", "high", "low", "volume"))

    tr = pd.concat([high - low, (high - close.shift()).abs(),
                    (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
    atr_pct = (atr / close).to_numpy()

    # Wilder ADX.
    up, down = high.diff(), -low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    atr_w = tr.ewm(alpha=1 / 14, adjust=False).mean().replace(0, np.nan)
    plus_di = 100 * plus_dm.ewm(alpha=1 / 14, adjust=False).mean() / atr_w
    minus_di = 100 * minus_dm.ewm(alpha=1 / 14, adjust=False).mean() / atr_w
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=1 / 14, adjust=False).mean().fillna(0).to_numpy()

    # Volatility-adaptive periods, exactly like the live strategy:
    # >3% ATR -> 7/14, 1-3% -> 9/21, <1% -> 13/34. Compute all three pairs,
    # then select per bar.
    emas = {p: close.ewm(span=p, adjust=False).mean().to_numpy()
            for p in (7, 14, 9, 21, 13, 34)}
    hi_vol, mid_vol = atr_pct > 0.03, (atr_pct > 0.01) & (atr_pct <= 0.03)
    fast = np.select([hi_vol, mid_vol], [emas[7], emas[9]], default=emas[13])
    slow = np.select([hi_vol, mid_vol], [emas[14], emas[21]], default=emas[34])

    above = fast > slow
    prev_above = np.roll(above, 1); prev_above[0] = above[0]
    cross_up = above & ~prev_above
    cross_dn = ~above & prev_above

    volr = (vol / vol.rolling(20).mean().replace(0, np.nan)).fillna(1.0).to_numpy()

    return {
        "open": df["open"].astype(float).to_numpy(),
        "high": high.to_numpy(), "low": low.to_numpy(), "close": close.to_numpy(),
        "atr": atr.to_numpy(), "adx": adx, "volr": volr,
        "cross_up": cross_up, "cross_dn": cross_dn, "n": len(df),
    }


def ema_simulate(F: dict, stop_atr: float, tp_atr: float, adx_min: float) -> np.ndarray:
    """One geometry over precomputed 1h features. Exits: stop/TP intrabar
    (stop checked first), bearish cross at close, safety cap on hold time.
    Costs: spot taker both sides + slippage both sides."""
    candidates = np.flatnonzero(F["cross_up"] & (F["adx"] > adx_min) & (F["volr"] > 1.0))
    o, h, l, c = F["open"], F["high"], F["low"], F["close"]
    n = F["n"]
    cost = 2 * (SPOT_TAKER + SLIP)

    rets, busy_until = [], -1
    for i in candidates:
        if i <= busy_until or i + 1 >= n:
            continue
        entry = o[i + 1] * (1 + SLIP)
        stop = entry - F["atr"][i] * stop_atr
        tp = entry + F["atr"][i] * tp_atr

        exit_px, j = None, i + 1
        last = min(i + 1 + EMA_MAX_HOLD_BARS, n - 1)
        for j in range(i + 1, last + 1):
            if l[j] <= stop:
                exit_px = stop
                break
            if h[j] >= tp:
                exit_px = tp
                break
            if F["cross_dn"][j]:
                exit_px = c[j]
                break
        if exit_px is None:
            exit_px = c[last]

        rets.append((exit_px - entry) / entry - cost)
        busy_until = j

    return np.array(rets)


def sweep_ema(frames: dict[str, pd.DataFrame]) -> list[dict]:
    pre = {}
    for sym, df in frames.items():
        logger.info(f"Precomputing 1h features for {sym} ({len(df):,} bars)")
        pre[sym] = ema_precompute(df)

    results = []
    for stop_atr, tp_atr, adx_min in itertools.product(*EMA_GRID.values()):
        all_rets, fold_nets = [], [0.0, 0.0, 0.0]
        for F in pre.values():
            rets = ema_simulate(F, stop_atr, tp_atr, adx_min)
            all_rets.append(rets)
            if len(rets):
                thirds = np.array_split(rets, 3)
                for fi in range(3):
                    fold_nets[fi] += float(thirds[fi].sum()) if len(thirds[fi]) else 0.0
        rets = np.concatenate(all_rets) if all_rets else np.array([])
        s = score(rets)
        s.update({"stop_atr": stop_atr, "tp_atr": tp_atr, "adx_min": adx_min,
                  "folds_positive": sum(1 for f in fold_nets if f > 0)})
        results.append(s)
    return results


# ---------------------------------------------------------------------- #
# Grid-strategy sweep (1h) — mirrors strategies/grid.py: ATR-derived level
# spacing, buy dips onto grid levels in ranging markets, TP one spacing up,
# grid killed when ADX says the market is trending.

def grid_simulate(F: dict, spacing_mult: float, stop_mult: float,
                  tp_mult: float, adx_kill: float) -> np.ndarray:
    """Reuses ema_precompute's features (atr, adx, ohlc)."""
    o, h, l, c = F["open"], F["high"], F["low"], F["close"]
    atr, adx = F["atr"], F["adx"]
    n = F["n"]
    cost = 2 * (SPOT_TAKER + SLIP)

    rets = []
    center, filled, busy_until = None, set(), -1
    for i in range(50, n - 1):
        if adx[i] > adx_kill:
            center, filled = None, set()
            continue
        price = c[i]
        spacing = min(max(atr[i] / price * spacing_mult, 0.002), 0.015)
        if center is None or abs(price - center) / price > spacing * 3:
            center, filled = price, set()
            continue
        # nearest unfilled level below price
        k = int(np.floor(np.log(price / center) / np.log(1 + spacing)))
        level = center * (1 + spacing) ** k
        if level >= price or k in filled or not (-6 <= k <= 6):
            continue
        if abs(price - level) / price >= spacing * 0.5:
            continue
        if i <= busy_until:
            continue
        filled.add(k)

        entry = o[i + 1] * (1 + SLIP)
        stop = entry * (1 - spacing * stop_mult)
        tp = level * (1 + spacing * tp_mult)
        exit_px, j = None, i + 1
        last = min(i + 1 + 500, n - 1)
        for j in range(i + 1, last + 1):
            if adx[j] > adx_kill:
                exit_px = c[j]
                break
            if l[j] <= stop:
                exit_px = stop
                break
            if h[j] >= tp:
                exit_px = tp
                break
        if exit_px is None:
            exit_px = c[last]
        rets.append((exit_px - entry) / entry - cost)
        busy_until = j
    return np.array(rets)


def sweep_grid(frames: dict[str, pd.DataFrame]) -> list[dict]:
    pre = {sym: ema_precompute(df) for sym, df in frames.items()}
    results = []
    for sp, st, tp, ak in itertools.product(*GRID_GRID.values()):
        all_rets, fold_nets = [], [0.0, 0.0, 0.0]
        for F in pre.values():
            rets = grid_simulate(F, sp, st, tp, ak)
            all_rets.append(rets)
            if len(rets):
                thirds = np.array_split(rets, 3)
                for fi in range(3):
                    fold_nets[fi] += float(thirds[fi].sum()) if len(thirds[fi]) else 0.0
        rets = np.concatenate(all_rets) if all_rets else np.array([])
        s = score(rets)
        s.update({"spacing_mult": sp, "stop_mult": st, "tp_mult": tp, "adx_kill": ak,
                  "folds_positive": sum(1 for f in fold_nets if f > 0)})
        results.append(s)
    return results


def pick_best(results: list[dict], min_trades: int = 60) -> dict | None:
    """Robust winner: enough trades, profitable overall, and profitable in at
    least 2 of 3 walk-forward folds. Rank by net, tie-break by profit factor."""
    ok = [r for r in results
          if r["n"] >= min_trades and r["net_bps"] > 0 and r["folds_positive"] >= 2]
    if not ok:
        return None
    return sorted(ok, key=lambda r: (r["net_bps"], r["pf"]), reverse=True)[0]


# ---------------------------------------------------------------------- #

def _print_table(results: list[dict], param_keys: list[str], title: str):
    top = sorted(results, key=lambda r: r["net_bps"], reverse=True)[:10]
    hdr = "".join(f"{k:>10}" for k in param_keys) + \
          f"{'trades':>8}{'win%':>7}{'net_bps':>9}{'PF':>7}{'folds+':>7}"
    print(f"\n{title}\n{hdr}\n" + "-" * len(hdr))
    for r in top:
        print("".join(f"{r[k]:>10}" for k in param_keys) +
              f"{r['n']:>8}{r['wr']:>7}{r['net_bps']:>9}{r['pf']:>7}"
              f"{r['folds_positive']:>7}")


def _load_frames(symbols: list[str], months: int, timeframe: str, market: str) -> dict:
    from backtest.data import load_history
    frames = {}
    for sym in symbols:
        df = load_history(sym, months=months, timeframe=timeframe, market=market)
        if not df.empty:
            frames[sym] = df
    return frames


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    ap = argparse.ArgumentParser(description="Strategy parameter optimizer")
    ap.add_argument("--strategy", choices=["scalp", "ema_cross", "grid", "all"], default="all")
    ap.add_argument("--symbols", default="BTC/USDT,ETH/USDT",
                    help="symbols for the scalp (1m) sweep")
    ap.add_argument("--swing-symbols", default="BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT",
                    help="symbols for the ema_cross (1h) sweep")
    ap.add_argument("--months", type=int, default=3, help="months of 1m data (scalp)")
    ap.add_argument("--swing-months", type=int, default=12, help="months of 1h data (ema)")
    ap.add_argument("--entry", choices=["maker", "taker"], default="maker")
    ap.add_argument("--apply", action="store_true",
                    help="write winners to config/tuning.json")
    args = ap.parse_args()

    winners: dict[str, dict] = {}
    meta: dict[str, dict] = {}

    # -- scalp sweep (1m) ------------------------------------------------- #
    if args.strategy in ("scalp", "all"):
        syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
        frames = _load_frames(syms, args.months, "1m", "futures")
        if frames:
            results = sweep(frames, entry=args.entry)
            _print_table(results, ["min_edge_mult", "tp_mult", "sl_mult"],
                         f"SCALP — top 10 of {len(results)} geometries "
                         f"(entry={args.entry}, {args.months}mo 1m)")
            best = pick_best(results, min_trades=60)
            if best:
                winners["scalp"] = {k: best[k] for k in ("min_edge_mult", "tp_mult", "sl_mult")}
                meta["scalp"] = {k: best[k] for k in ("n", "wr", "net_bps", "pf", "folds_positive")}
                print(f"SCALP WINNER: {winners['scalp']} -> {meta['scalp']}")
            else:
                print("SCALP: no geometry survived the robustness bar "
                      "(>=60 trades, profitable overall AND in 2/3 folds) — "
                      "keeping current params.")
        else:
            print("SCALP: no 1m history downloaded — skipped.")

    # -- ema_cross sweep (1h) ---------------------------------------------- #
    if args.strategy in ("ema_cross", "all"):
        syms = [s.strip() for s in args.swing_symbols.split(",") if s.strip()]
        frames = _load_frames(syms, args.swing_months, "1h", "spot")
        if frames:
            results = sweep_ema(frames)
            _print_table(results, ["stop_atr", "tp_atr", "adx_min"],
                         f"EMA_CROSS — top 10 of {len(results)} geometries "
                         f"({args.swing_months}mo 1h, {len(frames)} symbols)")
            best = pick_best(results, min_trades=25)   # swing trades are rarer
            if best:
                winners["ema_cross"] = {k: best[k] for k in ("stop_atr", "tp_atr", "adx_min")}
                meta["ema_cross"] = {k: best[k] for k in ("n", "wr", "net_bps", "pf", "folds_positive")}
                print(f"EMA_CROSS WINNER: {winners['ema_cross']} -> {meta['ema_cross']}")
            else:
                print("EMA_CROSS: no geometry survived the robustness bar "
                      "(>=25 trades, profitable overall AND in 2/3 folds) — "
                      "keeping current params.")
        else:
            print("EMA_CROSS: no 1h history downloaded — skipped.")

    # -- grid sweep (1h, same frames as ema) -------------------------------- #
    if args.strategy in ("grid", "all"):
        syms = [s.strip() for s in args.swing_symbols.split(",") if s.strip()]
        frames = _load_frames(syms, args.swing_months, "1h", "spot")
        if frames:
            results = sweep_grid(frames)
            _print_table(results, ["spacing_mult", "stop_mult", "tp_mult", "adx_kill"],
                         f"GRID — top 10 of {len(results)} geometries "
                         f"({args.swing_months}mo 1h, {len(frames)} symbols)")
            best = pick_best(results, min_trades=40)
            if best:
                winners["grid"] = {k: best[k] for k in
                                   ("spacing_mult", "stop_mult", "tp_mult", "adx_kill")}
                meta["grid"] = {k: best[k] for k in ("n", "wr", "net_bps", "pf", "folds_positive")}
                print(f"GRID WINNER: {winners['grid']} -> {meta['grid']}")
            else:
                print("GRID: no geometry survived the robustness bar — keeping current params.")
        else:
            print("GRID: no 1h history downloaded — skipped.")

    if not winners:
        print("\nNothing to apply.")
        return 1

    if args.apply:
        # Merge with any existing tuning so one sweep doesn't erase another's.
        existing = {}
        if _TUNING_PATH.exists():
            try:
                existing = json.loads(_TUNING_PATH.read_text(encoding="utf-8")) or {}
            except Exception:
                existing = {}
        existing.update(winners)
        existing["meta"] = {**existing.get("meta", {}),
                            **{k: {"source": "backtest.optimize", "evidence": v}
                               for k, v in meta.items()}}
        _TUNING_PATH.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        print(f"\nWritten to {_TUNING_PATH} — restart the bot to apply.")
    else:
        print("\n(Not applied. Re-run with --apply to write config/tuning.json.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
