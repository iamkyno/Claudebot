import logging
from typing import Optional
import pandas as pd
from strategies.base import BaseStrategy, Signal
from data.features import compute_features

logger = logging.getLogger(__name__)


class EMACrossStrategy(BaseStrategy):
    """EMA crossover trend-following strategy."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.name = "ema_cross"

    def generate_signal(self, symbol: str, df: pd.DataFrame, **kwargs) -> Optional[Signal]:
        if len(df) < 55:
            return None

        df = compute_features(df)
        last = df.iloc[-1]
        prev = df.iloc[-2]

        if pd.isna(last.get("ema_9")) or pd.isna(last.get("ema_21")):
            return None

        price = float(last["close"])
        atr = float(last["atr"]) if pd.notna(last.get("atr")) else price * 0.01
        adx = float(last["adx"]) if pd.notna(last.get("adx")) else 0.0

        features = {
            "rsi": float(last["rsi"]) if pd.notna(last.get("rsi")) else None,
            "ema_9": float(last["ema_9"]), "ema_21": float(last["ema_21"]),
            "ema_50": float(last["ema_50"]) if pd.notna(last.get("ema_50")) else None,
            "adx": adx, "atr": atr,
            "volume_ratio": float(last["volume_ratio"]) if pd.notna(last.get("volume_ratio")) else None,
            "macd": float(last["macd"]) if pd.notna(last.get("macd")) else None,
        }

        bullish_cross = float(prev["ema_9"]) <= float(prev["ema_21"]) and float(last["ema_9"]) > float(last["ema_21"])
        bearish_cross = float(prev["ema_9"]) >= float(prev["ema_21"]) and float(last["ema_9"]) < float(last["ema_21"])
        trending = adx > 20

        if bullish_cross and trending:
            stop = round(price - atr * 2.0, 8)
            tp = round(price + atr * 4.0, 8)
            return Signal(symbol=symbol, strategy=self.name, signal_type="buy",
                          confidence=0.65, stop_loss=stop, take_profit=tp, features=features)

        if bearish_cross:
            return Signal(symbol=symbol, strategy=self.name, signal_type="sell",
                          confidence=0.60, stop_loss=0.0, take_profit=0.0, features=features)

        return None

    def should_exit(self, symbol: str, df: pd.DataFrame, trade: dict) -> bool:
        if len(df) < 55:
            return False
        df = compute_features(df)
        last = df.iloc[-1]
        price = float(last["close"])

        if pd.notna(last.get("ema_9")) and pd.notna(last.get("ema_21")):
            if float(last["ema_9"]) < float(last["ema_21"]):
                return True
        if trade.get("stop_loss") and price <= float(trade["stop_loss"]):
            return True
        if trade.get("take_profit") and price >= float(trade["take_profit"]):
            return True
        return False
