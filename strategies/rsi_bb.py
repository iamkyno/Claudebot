import logging
from typing import Optional
import pandas as pd
from strategies.base import BaseStrategy, Signal
from data.features import compute_features

logger = logging.getLogger(__name__)


class RSIBBStrategy(BaseStrategy):
    """
    RSI + Bollinger Band mean-reversion.
    Thresholds are computed from the data distribution (20th/80th RSI
    percentile over the last 200 candles) so no manual tuning is needed.
    Only fires in ranging markets (ADX < 25).
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.name = "rsi_bb"
        self.atr_stop_mult = config.get("atr_stop_multiplier", 1.5)

    # ------------------------------------------------------------------ #

    def generate_signal(self, symbol: str, df: pd.DataFrame, **kwargs) -> Optional[Signal]:
        if len(df) < 60:
            return None

        df = compute_features(df)
        last = df.iloc[-1]

        rsi = last.get("rsi")
        bb_pos = last.get("bb_position")
        adx = float(last["adx"]) if pd.notna(last.get("adx")) else 0.0

        if pd.isna(rsi) or pd.isna(bb_pos):
            return None

        # Only run in ranging market
        if adx >= 25:
            return None

        # Adaptive thresholds from recent RSI distribution
        oversold, overbought = self._adaptive_thresholds(df)

        price = float(last["close"])
        atr = float(last["atr"]) if pd.notna(last.get("atr")) else price * 0.01
        vol_ratio = float(last["volume_ratio"]) if pd.notna(last.get("volume_ratio")) else 1.0

        features = {
            "rsi": float(rsi), "bb_position": float(bb_pos), "adx": adx,
            "macd": float(last["macd"]) if pd.notna(last.get("macd")) else None,
            "macd_signal": float(last["macd_signal"]) if pd.notna(last.get("macd_signal")) else None,
            "volume_ratio": vol_ratio, "atr": atr,
            "price_change_1h": float(last["price_change_1h"]) if pd.notna(last.get("price_change_1h")) else None,
            "price_change_4h": float(last["price_change_4h"]) if pd.notna(last.get("price_change_4h")) else None,
            "price_change_24h": float(last["price_change_24h"]) if pd.notna(last.get("price_change_24h")) else None,
        }

        # Buy: adaptive oversold + lower BB + volume spike
        if float(rsi) < oversold and float(bb_pos) < 0.15 and vol_ratio > 1.1:
            stop = round(price - atr * self.atr_stop_mult, 8)
            tp = round(price + atr * 3.0, 8)
            conf = min(0.50 + (oversold - float(rsi)) / oversold, 0.95)
            return Signal(symbol=symbol, strategy=self.name, signal_type="buy",
                          confidence=conf, stop_loss=stop, take_profit=tp, features=features)

        # Sell / exit alert: adaptive overbought + upper BB
        if float(rsi) > overbought and float(bb_pos) > 0.85:
            conf = min(0.50 + (float(rsi) - overbought) / (100 - overbought), 0.95)
            return Signal(symbol=symbol, strategy=self.name, signal_type="sell",
                          confidence=conf, stop_loss=0.0, take_profit=0.0, features=features)

        return None

    def should_exit(self, symbol: str, df: pd.DataFrame, trade: dict) -> bool:
        if len(df) < 20:
            return False
        df = compute_features(df)
        last = df.iloc[-1]
        price = float(last["close"])

        _, overbought = self._adaptive_thresholds(df)
        if pd.notna(last.get("rsi")) and float(last["rsi"]) > overbought * 0.9:
            return True
        if trade.get("stop_loss") and price <= float(trade["stop_loss"]):
            return True
        if trade.get("take_profit") and price >= float(trade["take_profit"]):
            return True
        return False

    # ------------------------------------------------------------------ #

    @staticmethod
    def _adaptive_thresholds(df: pd.DataFrame) -> tuple[float, float]:
        """20th / 80th RSI percentile over last 200 candles."""
        rsi_series = df["rsi"].dropna().tail(200)
        if len(rsi_series) < 40:
            return 30.0, 70.0
        return float(rsi_series.quantile(0.20)), float(rsi_series.quantile(0.80))
