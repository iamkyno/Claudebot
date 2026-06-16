import logging
from typing import Optional
import pandas as pd
from strategies.base import BaseStrategy, Signal
from data.features import compute_features

logger = logging.getLogger(__name__)


class RSIBBStrategy(BaseStrategy):
    """RSI + Bollinger Band mean-reversion strategy."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.name = "rsi_bb"
        self.rsi_oversold = config.get("rsi_oversold", 30)
        self.rsi_overbought = config.get("rsi_overbought", 70)
        self.atr_stop_mult = config.get("atr_stop_multiplier", 1.5)

    def generate_signal(self, symbol: str, df: pd.DataFrame, **kwargs) -> Optional[Signal]:
        if len(df) < 50:
            return None

        df = compute_features(df)
        last = df.iloc[-1]

        rsi = last.get("rsi")
        bb_pos = last.get("bb_position")
        if pd.isna(rsi) or pd.isna(bb_pos):
            return None

        price = float(last["close"])
        atr = float(last["atr"]) if pd.notna(last.get("atr")) else price * 0.01
        vol_ratio = float(last["volume_ratio"]) if pd.notna(last.get("volume_ratio")) else 1.0

        features = {
            "rsi": float(rsi), "bb_position": float(bb_pos),
            "macd": float(last["macd"]) if pd.notna(last.get("macd")) else None,
            "macd_signal": float(last["macd_signal"]) if pd.notna(last.get("macd_signal")) else None,
            "volume_ratio": vol_ratio, "atr": atr,
            "price_change_1": float(last["price_change_1"]) if pd.notna(last.get("price_change_1")) else None,
            "price_change_4": float(last["price_change_4"]) if pd.notna(last.get("price_change_4")) else None,
            "price_change_24": float(last["price_change_24"]) if pd.notna(last.get("price_change_24")) else None,
        }

        # Buy: RSI oversold + price at lower BB + volume confirmation
        if float(rsi) < self.rsi_oversold and float(bb_pos) < 0.15 and vol_ratio > 1.1:
            stop = round(price - atr * self.atr_stop_mult, 8)
            tp = round(price + atr * 3.0, 8)
            conf = min(0.50 + (self.rsi_oversold - float(rsi)) / 100, 0.95)
            return Signal(symbol=symbol, strategy=self.name, signal_type="buy",
                          confidence=conf, stop_loss=stop, take_profit=tp, features=features)

        # Sell signal (exit / short alert)
        if float(rsi) > self.rsi_overbought and float(bb_pos) > 0.85:
            conf = min(0.50 + (float(rsi) - self.rsi_overbought) / 100, 0.95)
            return Signal(symbol=symbol, strategy=self.name, signal_type="sell",
                          confidence=conf, stop_loss=0.0, take_profit=0.0, features=features)

        return None

    def should_exit(self, symbol: str, df: pd.DataFrame, trade: dict) -> bool:
        if len(df) < 20:
            return False
        df = compute_features(df)
        last = df.iloc[-1]
        price = float(last["close"])

        if pd.notna(last.get("rsi")) and float(last["rsi"]) > 65:
            return True
        if trade.get("stop_loss") and price <= float(trade["stop_loss"]):
            return True
        if trade.get("take_profit") and price >= float(trade["take_profit"]):
            return True
        return False
