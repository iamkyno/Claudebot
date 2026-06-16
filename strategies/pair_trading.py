import logging
from typing import Optional
import numpy as np
import pandas as pd
from strategies.base import BaseStrategy, Signal

logger = logging.getLogger(__name__)


class PairTradingStrategy(BaseStrategy):
    """
    Statistical pair trading on the two most-liquid USDT pairs (BTC/ETH by default).
    Lookback and z-score entry threshold are auto-derived from the spread's
    autocorrelation structure rather than configured manually.
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.name = "pair_trading"
        self.symbols = ["BTC/USDT", "ETH/USDT"]  # highest-correlation pair
        self.atr_stop_mult = config.get("atr_stop_multiplier", 1.5)

    def generate_signal(self, symbol: str, df: pd.DataFrame, **kwargs) -> Optional[Signal]:
        df_b = kwargs.get("df_secondary")
        if df_b is None or len(df) < 50 or len(df_b) < 50:
            return None

        price_a = float(df.iloc[-1]["close"])
        atr = float(df.iloc[-1]["atr"]) if pd.notna(df.iloc[-1].get("atr")) else price_a * 0.01

        # Auto-compute optimal lookback from mean-reversion half-life
        lookback = self._half_life_lookback(df, df_b)
        lookback = max(20, min(lookback, 120))

        log_a = np.log(df["close"].tail(lookback).values)
        log_b = np.log(df_b["close"].tail(lookback).values)
        spread = log_a - log_b

        mean, std = spread.mean(), spread.std()
        if std < 1e-8:
            return None

        z = (spread[-1] - mean) / std

        # Dynamic entry threshold: 1.5 if mean-reverts fast, 2.5 if slow
        entry_z = self._entry_z(lookback)

        features = {
            "zscore": float(z), "spread": float(spread[-1]),
            "spread_mean": float(mean), "spread_std": float(std),
            "lookback_used": lookback, "entry_z": entry_z,
        }

        if z > entry_z:
            # A overpriced vs B → exit or avoid
            return Signal(
                symbol=self.symbols[0], strategy=self.name, signal_type="sell",
                confidence=min(0.50 + (z - entry_z) / 4.0, 0.92),
                stop_loss=round(price_a * (1 + atr / price_a * self.atr_stop_mult), 8),
                take_profit=round(price_a * (1 - atr / price_a * self.atr_stop_mult), 8),
                features=features, metadata={"pair": self.symbols, "zscore": z},
            )

        if z < -entry_z:
            # A underpriced vs B → buy A
            return Signal(
                symbol=self.symbols[0], strategy=self.name, signal_type="buy",
                confidence=min(0.50 + (abs(z) - entry_z) / 4.0, 0.92),
                stop_loss=round(price_a - atr * self.atr_stop_mult, 8),
                take_profit=round(price_a + atr * self.atr_stop_mult * 2.0, 8),
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

    # ------------------------------------------------------------------ #

    @staticmethod
    def _half_life_lookback(df_a: pd.DataFrame, df_b: pd.DataFrame) -> int:
        """
        Estimate mean-reversion half-life via AR(1) on the log-price spread.
        Returns a lookback period calibrated to the spread's persistence.
        """
        try:
            n = min(200, len(df_a), len(df_b))
            spread = (np.log(df_a["close"].tail(n).values) -
                      np.log(df_b["close"].tail(n).values))
            delta = np.diff(spread)
            lag = spread[:-1] - spread[:-1].mean()
            if np.std(lag) < 1e-10:
                return 30
            beta = np.dot(lag, delta) / np.dot(lag, lag)
            if beta >= 0 or beta <= -1:
                return 30
            half_life = int(-np.log(2) / np.log(1 + beta))
            return max(15, min(half_life * 2, 120))
        except Exception:
            return 30

    @staticmethod
    def _entry_z(lookback: int) -> float:
        """Wider z-threshold for longer lookbacks (slower mean-reversion)."""
        if lookback <= 25:
            return 1.8
        if lookback <= 50:
            return 2.0
        return 2.3
