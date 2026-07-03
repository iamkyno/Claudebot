import logging
from typing import Optional
import pandas as pd
import ta
from strategies.base import BaseStrategy, Signal
from data.features import compute_features

logger = logging.getLogger(__name__)


class EMACrossStrategy(BaseStrategy):
    """
    EMA crossover trend-following strategy.
    EMA periods are automatically selected based on the asset's
    current volatility regime (ATR/price ratio):
      - High volatility  (>3%)  → 7/14 EMAs  (faster reaction)
      - Medium volatility (1-3%) → 9/21 EMAs
      - Low volatility   (<1%)  → 13/34 EMAs (smoother signals)
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.name = "ema_cross"
        self.atr_stop_mult = config.get("atr_stop_multiplier", 1.5)
        # Bracket + trend-strength gate — tunable, overridden by
        # config/tuning.json when the optimizer has found better values on
        # historical data (python -m backtest.optimize --apply).
        self.stop_atr = float(config.get("stop_atr", self.atr_stop_mult * 1.5))
        self.tp_atr = float(config.get("tp_atr", self.atr_stop_mult * 3.0))
        self.adx_min = float(config.get("adx_min", 20))

    # ------------------------------------------------------------------ #

    def generate_signal(self, symbol: str, df: pd.DataFrame, **kwargs) -> Optional[Signal]:
        if len(df) < 60:
            return None

        df = compute_features(df)
        last = df.iloc[-1]
        prev = df.iloc[-2]

        price = float(last["close"])
        atr = float(last["atr"]) if pd.notna(last.get("atr")) else price * 0.01
        adx = float(last["adx"]) if pd.notna(last.get("adx")) else 0.0
        vol_ratio = atr / price

        fast_p, slow_p = self._ema_periods(vol_ratio)
        fast_col, slow_col = f"_ema_f{fast_p}", f"_ema_s{slow_p}"

        # Compute adaptive EMAs on the fly
        df[fast_col] = ta.trend.EMAIndicator(df["close"], window=fast_p).ema_indicator()
        df[slow_col] = ta.trend.EMAIndicator(df["close"], window=slow_p).ema_indicator()

        last = df.iloc[-1]
        prev = df.iloc[-2]

        if pd.isna(last.get(fast_col)) or pd.isna(last.get(slow_col)):
            return None

        features = {
            "rsi": float(last["rsi"]) if pd.notna(last.get("rsi")) else None,
            "ema_9": float(last[fast_col]), "ema_21": float(last[slow_col]),
            "ema_50": float(last["ema_50"]) if pd.notna(last.get("ema_50")) else None,
            "adx": adx, "atr": atr,
            "volume_ratio": float(last["volume_ratio"]) if pd.notna(last.get("volume_ratio")) else None,
            "macd": float(last["macd"]) if pd.notna(last.get("macd")) else None,
        }

        bullish_cross = (float(prev[fast_col]) <= float(prev[slow_col]) and
                         float(last[fast_col]) > float(last[slow_col]))
        bearish_cross = (float(prev[fast_col]) >= float(prev[slow_col]) and
                         float(last[fast_col]) < float(last[slow_col]))

        # Volume confirmation: above 20-period average
        vol_confirming = (float(last["volume_ratio"]) > 1.0
                          if pd.notna(last.get("volume_ratio")) else True)

        if bullish_cross and adx > self.adx_min and vol_confirming:
            stop = round(price - atr * self.stop_atr, 8)
            tp = round(price + atr * self.tp_atr, 8)
            conf = min(0.60 + adx / 200, 0.90)
            return Signal(symbol=symbol, strategy=self.name, signal_type="buy",
                          confidence=conf, stop_loss=stop, take_profit=tp, features=features)

        if bearish_cross:
            return Signal(symbol=symbol, strategy=self.name, signal_type="sell",
                          confidence=0.60, stop_loss=0.0, take_profit=0.0, features=features)

        return None

    def should_exit(self, symbol: str, df: pd.DataFrame, trade: dict) -> bool:
        if len(df) < 60:
            return False
        df = compute_features(df)
        last = df.iloc[-1]
        price = float(last["close"])
        atr = float(last["atr"]) if pd.notna(last.get("atr")) else price * 0.01
        vol_ratio = atr / price
        fast_p, slow_p = self._ema_periods(vol_ratio)

        df[f"_ef"] = ta.trend.EMAIndicator(df["close"], window=fast_p).ema_indicator()
        df[f"_es"] = ta.trend.EMAIndicator(df["close"], window=slow_p).ema_indicator()
        last = df.iloc[-1]

        if pd.notna(last["_ef"]) and pd.notna(last["_es"]):
            if float(last["_ef"]) < float(last["_es"]):
                return True
        if trade.get("stop_loss") and price <= float(trade["stop_loss"]):
            return True
        if trade.get("take_profit") and price >= float(trade["take_profit"]):
            return True
        return False

    # ------------------------------------------------------------------ #

    @staticmethod
    def _ema_periods(vol_ratio: float) -> tuple[int, int]:
        if vol_ratio > 0.03:
            return 7, 14
        if vol_ratio > 0.01:
            return 9, 21
        return 13, 34
