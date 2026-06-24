"""
Market regime classifier.

Every strategy has a regime where it works and regimes where it bleeds:
  - Trend-following (EMA cross) wants TRENDING, dies in RANGING chop.
  - Mean-reversion (RSI+BB) wants RANGING, gets run over in strong TRENDS.

This module reads the indicators already computed in data/features.py (ADX,
realized volatility, Bollinger width) and labels the current bar so the
orchestrator can gate each strategy to the regime it was built for.
"""

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Regime labels
TRENDING = "trending"
RANGING = "ranging"
HIGH_VOL = "high_vol"
LOW_VOL = "low_vol"


def classify_regime(df: pd.DataFrame) -> dict:
    """
    Return a dict describing the current market regime.

    {
      "trend": "trending" | "ranging",
      "vol":   "high_vol" | "low_vol" | "normal",
      "adx":   float, "bb_width": float, "realized_vol": float,
      "direction": "up" | "down" | "flat",
    }
    """
    if df is None or len(df) < 30:
        return {"trend": RANGING, "vol": "normal", "adx": 0.0,
                "bb_width": 0.0, "realized_vol": 0.0, "direction": "flat"}

    last = df.iloc[-1]

    adx = float(last["adx"]) if pd.notna(last.get("adx")) else 0.0
    bb_width = float(last["bb_width"]) if pd.notna(last.get("bb_width")) else 0.0

    # Realized volatility: std of last-24 log returns, annualised-ish.
    closes = df["close"].astype(float).tail(25).values
    rets = np.diff(np.log(closes)) if len(closes) > 1 else np.array([0.0])
    realized_vol = float(np.std(rets)) if len(rets) else 0.0

    # Volatility percentile vs the strategy's own recent history (last ~200).
    vol_series = (
        df["close"].astype(float).pct_change().rolling(24).std().dropna().tail(200)
    )
    if len(vol_series) >= 20:
        hi = float(vol_series.quantile(0.75))
        lo = float(vol_series.quantile(0.25))
        cur = float(vol_series.iloc[-1])
        vol = HIGH_VOL if cur >= hi else (LOW_VOL if cur <= lo else "normal")
    else:
        vol = "normal"

    # Trend vs range: ADX is the workhorse. >25 = trending, <20 = ranging.
    trend = TRENDING if adx >= 25 else RANGING

    # Direction from EMA stack.
    direction = "flat"
    if pd.notna(last.get("ema_21")) and pd.notna(last.get("ema_50")):
        if float(last["ema_21"]) > float(last["ema_50"]):
            direction = "up"
        elif float(last["ema_21"]) < float(last["ema_50"]):
            direction = "down"

    return {
        "trend": trend, "vol": vol, "adx": adx, "bb_width": bb_width,
        "realized_vol": realized_vol, "direction": direction,
    }


# Which regime each strategy wants. The orchestrator skips a strategy when the
# live regime isn't in its allow-list. Strategies not listed run in any regime.
STRATEGY_REGIME = {
    "ema_cross":  {TRENDING},
    "rsi_bb":     {RANGING},
    "grid":       {RANGING},
    # funding_rate, liquidation_cascade, pair_trading, scalp: regime-agnostic
}


def regime_allows(strategy: str, regime: dict) -> bool:
    """True if `strategy` is allowed to trade in the current regime."""
    allowed = STRATEGY_REGIME.get(strategy)
    if not allowed:
        return True
    return regime.get("trend") in allowed
