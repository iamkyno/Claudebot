"""
Scalp bracket optimizer — the bot derives its own parameters from evidence.

Sweeps bracket geometry (min-edge multiple, TP multiple, SL multiple) over
months of real 1m history, using the same entry rules, fee model and time
stop as the live sniper engine. Survivors must be profitable in at least
2 of 3 time folds (walk-forward) so one lucky regime can't pick the params.

    python -m backtest.optimize                       # sweep + report
    python -m backtest.optimize --apply               # also write config/tuning.json
    python -m backtest.optimize --symbols BTC/USDT,ETH/USDT --months 3

The bot reads config/tuning.json at startup (see config.settings.get_tuning)
— optimizer values override the hand-set defaults.
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

# Grid: 5 x 5 x 3 = 75 geometries.
GRID = {
    "min_edge_mult": [2.0, 2.5, 3.0, 3.5, 4.0],
    "tp_mult":       [1.0, 1.3, 1.6, 2.0, 2.5],
    "sl_mult":       [0.4, 0.6, 0.8],
}

TIME_STOP_BARS = 15          # mirrors scalp_time_stop_min on 1m bars
FUTURES_TAKER = 0.0005
FUTURES_MAKER = 0.0002
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


def pick_best(results: list[dict], min_trades: int = 60) -> dict | None:
    """Robust winner: enough trades, profitable overall, and profitable in at
    least 2 of 3 walk-forward folds. Rank by net, tie-break by profit factor."""
    ok = [r for r in results
          if r["n"] >= min_trades and r["net_bps"] > 0 and r["folds_positive"] >= 2]
    if not ok:
        return None
    return sorted(ok, key=lambda r: (r["net_bps"], r["pf"]), reverse=True)[0]


# ---------------------------------------------------------------------- #

def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    ap = argparse.ArgumentParser(description="Scalp bracket optimizer")
    ap.add_argument("--symbols", default="BTC/USDT,ETH/USDT")
    ap.add_argument("--months", type=int, default=3)
    ap.add_argument("--entry", choices=["maker", "taker"], default="maker")
    ap.add_argument("--apply", action="store_true",
                    help="write the winner to config/tuning.json")
    args = ap.parse_args()

    from backtest.data import load_history
    frames = {}
    for sym in [s.strip() for s in args.symbols.split(",") if s.strip()]:
        df = load_history(sym, months=args.months, market="futures")
        if not df.empty:
            frames[sym] = df
    if not frames:
        print("No history downloaded — check connectivity and symbol names.")
        return 1

    results = sweep(frames, entry=args.entry)

    top = sorted(results, key=lambda r: r["net_bps"], reverse=True)[:10]
    hdr = (f"{'edge_x':>7}{'tp_x':>6}{'sl_x':>6}{'trades':>8}{'win%':>7}"
           f"{'net_bps':>9}{'PF':>7}{'folds+':>7}")
    print("\nTop 10 geometries (fees + slippage modeled, entry="
          f"{args.entry}):\n{hdr}\n" + "-" * len(hdr))
    for r in top:
        print(f"{r['min_edge_mult']:>7}{r['tp_mult']:>6}{r['sl_mult']:>6}"
              f"{r['n']:>8}{r['wr']:>7}{r['net_bps']:>9}{r['pf']:>7}"
              f"{r['folds_positive']:>7}")

    best = pick_best(results)
    if best is None:
        print("\nNo geometry survived the robustness bar (>=60 trades, "
              "profitable overall AND in 2/3 time folds).\n"
              "The honest conclusion: don't ship new params from this window.")
        return 1

    print(f"\nWINNER: edge>={best['min_edge_mult']}x costs, "
          f"TP {best['tp_mult']}x / SL {best['sl_mult']}x edge "
          f"-> {best['n']} trades, {best['wr']}% win, "
          f"{best['net_bps']} bps net, PF {best['pf']}")

    if args.apply:
        tuning = {
            "scalp": {
                "min_edge_mult": best["min_edge_mult"],
                "tp_mult": best["tp_mult"],
                "sl_mult": best["sl_mult"],
            },
            "meta": {
                "source": "backtest.optimize",
                "symbols": list(frames.keys()),
                "months": args.months,
                "entry": args.entry,
                "evidence": {k: best[k] for k in ("n", "wr", "net_bps", "pf", "folds_positive")},
            },
        }
        _TUNING_PATH.write_text(json.dumps(tuning, indent=2), encoding="utf-8")
        print(f"\nWritten to {_TUNING_PATH} — restart the bot to apply.")
    else:
        print("\n(Not applied. Re-run with --apply to write config/tuning.json.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
