import logging
from typing import Optional
import numpy as np
import pandas as pd
from strategies.base import BaseStrategy, Signal

logger = logging.getLogger(__name__)


class PairTradingStrategy(BaseStrategy):
    """
    Statistical pair trading on BTC/ETH spread.
    Trade the z-score of the log price ratio when it diverges beyond threshold.
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.name = "pair_trading"
        self.symbols = config.get("symbols", ["BTC/USDT", "ETH/USDT"])
        self.lookback = config.get("lookback_periods", 30)
        self.entry_z = config.get("zscore_entry", 2.0)
        self.exit_z = config.get("zscore_exit", 0.5)

    def generate_signal(self, symbol: str, df: pd.DataFrame, **kwargs) -> Optional[Signal]:
        df_b = kwargs.get("df_secondary")
        if df_b is None or len(df) < self.lookback or len(df_b) < self.lookback:
            return None

        price_a = float(df.iloc[-1]["close"])
        log_a = np.log(df["close"].tail(self.lookback).values)
        log_b = np.log(df_b["close"].tail(self.lookback).values)
        spread = log_a - log_b

        mean, std = spread.mean(), spread.std()
        if std < 1e-8:
            return None

        z = (spread[-1] - mean) / std
        atr = float(df.iloc[-1]["atr"]) if pd.notna(df.iloc[-1].get("atr")) else price_a * 0.01

        features = {"zscore": float(z), "spread": float(spread[-1]),
                    "spread_mean": float(mean), "spread_std": float(std)}

        if z > self.entry_z:
            # A overpriced vs B → fade A
            return Signal(
                symbol=self.symbols[0], strategy=self.name, signal_type="sell",
                confidence=min(0.50 + (z - self.entry_z) / 4, 0.92),
                stop_loss=round(price_a * 1.03, 8), take_profit=round(price_a * 0.97, 8),
                features=features, metadata={"pair": self.symbols, "zscore": z},
            )

        if z < -self.entry_z:
            # A underpriced vs B → buy A
            return Signal(
                symbol=self.symbols[0], strategy=self.name, signal_type="buy",
                confidence=min(0.50 + (abs(z) - self.entry_z) / 4, 0.92),
                stop_loss=round(price_a - atr * 1.5, 8), take_profit=round(price_a + atr * 2.0, 8),
                features=features, metadata={"pair": self.symbols, "zscore": z},
            )

        return None

    def should_exit(self, symbol: str, df: pd.DataFrame, trade: dict) -> bool:
        price = float(df.iloc[-1]["close"])
        if trade.get("stop_loss") and price <= float(trade["stop_loss"]):
            return True
        if trade.get("take_profit") and price >= float(trade["take_profit"]):
            return True
        return False
