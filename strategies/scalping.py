import logging
from typing import Optional

import pandas as pd

from strategies.base import BaseStrategy, Signal
from data.features import compute_features

logger = logging.getLogger(__name__)


class ScalpStrategy(BaseStrategy):
    """
    Fee-aware futures scalper. Fast in, fast out.

    Entry signals are read on the fast timeframe (1m); a slower timeframe
    (5m) is used only as a trend gate so we never scalp against the higher
    timeframe. Long-only for this cut.

    Three independent micro-edges — whichever fires with the most conviction
    wins:

      1. VWAP reversion  — price stretched below the intraday VWAP and the
                           last candle is turning back up (mean reversion).
      2. Order-flow lean — strong resting bid imbalance in the book plus a
                           1m up-tick (buyers leaning on the bid).
      3. Momentum burst  — volume spike + price thrust we ride for a few
                           candles (continuation).

    THE KEY DISCIPLINE: every take-profit must clear ~2x the round-trip fee.
    Scalping dies on fees, so any setup whose realistic move can't beat costs
    is rejected outright. Targets and stops are expressed in basis points off
    entry, volatility-scaled, with a hard time-stop so dead trades are cut.
    """

    def __init__(self, config: dict, fee_rate: float = 0.0005,
                 timeframe: str = "1m"):
        super().__init__(config)
        self.name = "scalp"
        self.timeframe = timeframe
        self.fee_rate = fee_rate                 # one side (taker)
        self.round_trip = fee_rate * 2           # entry + exit
        # A target must clear at least this net edge (2x round-trip = comfort).
        self.min_edge = self.round_trip * 2
        self.time_stop_min = config.get("scalp_time_stop_min", 15)

    # ------------------------------------------------------------------ #

    def generate_signal(self, symbol: str, df: pd.DataFrame, **kwargs) -> Optional[Signal]:
        df_trend = kwargs.get("df_trend")
        ob_imbalance = float(kwargs.get("ob_imbalance", 0.5) or 0.5)

        if len(df) < 60:
            return None

        df = compute_features(df)
        last = df.iloc[-1]
        prev = df.iloc[-2]

        price = float(last["close"])
        atr = float(last["atr"]) if pd.notna(last.get("atr")) else price * 0.005
        atr_pct = atr / price if price else 0.0

        # Dead market: even a full ATR move can't clear a round trip — skip.
        if atr_pct < self.round_trip:
            return None

        # Higher-timeframe gate: only go long with the 5m trend.
        if not self._trend_ok(df_trend):
            return None

        vwap = float(last["vwap"]) if pd.notna(last.get("vwap")) else price
        rsi = float(last["rsi"]) if pd.notna(last.get("rsi")) else 50.0
        vol_ratio = float(last["volume_ratio"]) if pd.notna(last.get("volume_ratio")) else 1.0
        upticking = price > float(prev["close"])
        pc1 = float(last["price_change_1"]) if pd.notna(last.get("price_change_1")) else 0.0

        # Target edge: clears fees, scaled up by volatility so we don't set a
        # target inside the candle's own noise.
        edge = max(self.min_edge, atr_pct * 0.8)

        kind, conf = None, 0.0

        # 1) VWAP reversion — stretched below the mean and turning up.
        dev = (vwap - price) / price
        if dev > edge and rsi < 40 and upticking and vol_ratio > 1.0:
            conf = min(0.55 + (dev / edge) * 0.15, 0.90)
            kind = "vwap_revert"

        # 2) Order-flow lean — buyers stacked on the bid, price ticking up.
        elif ob_imbalance > 0.66 and upticking and rsi < 60:
            conf = min(0.55 + (ob_imbalance - 0.66) * 1.2, 0.90)
            kind = "order_flow"

        # 3) Momentum burst — volume spike with a real thrust to ride.
        elif vol_ratio > 2.5 and upticking and pc1 > edge:
            conf = min(0.55 + vol_ratio / 20.0, 0.88)
            kind = "momentum"

        if kind is None:
            return None

        # Fee-aware bracket. TP clears costs; SL tighter for >1:1 reward:risk.
        take_profit = round(price * (1 + edge), 8)
        stop_loss = round(price * (1 - edge * 0.6), 8)

        features = {
            "rsi": rsi,
            "macd": float(last["macd"]) if pd.notna(last.get("macd")) else None,
            "macd_signal": float(last["macd_signal"]) if pd.notna(last.get("macd_signal")) else None,
            "bb_position": float(last["bb_position"]) if pd.notna(last.get("bb_position")) else None,
            "ema_9": float(last["ema_9"]) if pd.notna(last.get("ema_9")) else None,
            "ema_21": float(last["ema_21"]) if pd.notna(last.get("ema_21")) else None,
            "ema_50": float(last["ema_50"]) if pd.notna(last.get("ema_50")) else None,
            "atr": atr,
            "volume_ratio": vol_ratio,
            "price_change_1": pc1,
            "price_change_4": float(last["price_change_4"]) if pd.notna(last.get("price_change_4")) else None,
            "price_change_24": float(last["price_change_24"]) if pd.notna(last.get("price_change_24")) else None,
        }

        logger.info(
            f"Scalp {symbol}: {kind} conf={conf:.2f} edge={edge*100:.2f}% "
            f"entry={price:.6f} tp={take_profit:.6f} sl={stop_loss:.6f}"
        )
        return Signal(
            symbol=symbol, strategy=self.name, signal_type="buy",
            confidence=conf, stop_loss=stop_loss, take_profit=take_profit,
            features=features, metadata={"kind": kind, "edge": edge},
        )

    # ------------------------------------------------------------------ #

    def should_exit(self, symbol: str, df: pd.DataFrame, trade: dict) -> bool:
        if len(df) < 20:
            return False

        df = compute_features(df)
        last = df.iloc[-1]
        price = float(last["close"])

        # Hard bracket first.
        if trade.get("stop_loss") and price <= float(trade["stop_loss"]):
            return True
        if trade.get("take_profit") and price >= float(trade["take_profit"]):
            return True

        # Mean reached: if we entered below VWAP and price has climbed back to
        # it, take the reversion profit rather than wait for the full target.
        vwap = float(last["vwap"]) if pd.notna(last.get("vwap")) else None
        entry = float(trade.get("entry_price") or price)
        if vwap and entry < vwap and price >= vwap:
            return True

        # Time stop: a scalp that hasn't worked quickly is dead money.
        entry_time = trade.get("entry_time")
        if entry_time is not None:
            try:
                held_min = (df.index[-1] - pd.to_datetime(entry_time)).total_seconds() / 60.0
                if held_min >= self.time_stop_min:
                    return True
            except Exception:
                pass

        return False

    # ------------------------------------------------------------------ #

    @staticmethod
    def _trend_ok(df_trend: Optional[pd.DataFrame]) -> bool:
        """Long only when the confirm timeframe is trending up (or unknown)."""
        if df_trend is None or len(df_trend) < 50:
            return True  # no confirm data — don't block
        t = compute_features(df_trend).iloc[-1]
        if pd.isna(t.get("ema_21")) or pd.isna(t.get("ema_50")):
            return True
        return (float(t["ema_21"]) >= float(t["ema_50"])
                and float(t["close"]) >= float(t["ema_21"]))
