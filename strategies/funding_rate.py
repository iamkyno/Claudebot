import logging
from typing import Optional
import pandas as pd
from strategies.base import BaseStrategy, Signal

logger = logging.getLogger(__name__)


class FundingRateStrategy(BaseStrategy):
    """
    Funding rate arbitrage on Binance perpetual futures.
    Minimum rate threshold is auto-set to 0.1% per 8h (≈10.95% APY) —
    below that the edge doesn't cover transaction costs.
    """

    _MIN_RATE = 0.001   # 0.1% per 8h — fixed lower bound, not configurable

    def __init__(self, config: dict):
        super().__init__(config)
        self.name = "funding_rate"

    def generate_signal(self, symbol: str, df: pd.DataFrame, **kwargs) -> Optional[Signal]:
        funding_rate = kwargs.get("funding_rate")
        if funding_rate is None:
            return None

        price = float(df.iloc[-1]["close"])
        features = {"funding_rate": funding_rate, "price": price}

        if funding_rate > self._MIN_RATE:
            conf = min(0.55 + funding_rate * 50, 0.90)
            return Signal(
                symbol=symbol, strategy=self.name, signal_type="buy",
                confidence=conf,
                stop_loss=round(price * 0.97, 8),
                take_profit=round(price * 1.05, 8),
                features=features,
                metadata={"trade_type": "funding_arb", "funding_rate": funding_rate},
            )

        if funding_rate < -self._MIN_RATE:
            conf = min(0.55 + abs(funding_rate) * 50, 0.90)
            return Signal(
                symbol=symbol, strategy=self.name, signal_type="sell",
                confidence=conf,
                stop_loss=round(price * 1.03, 8),
                take_profit=round(price * 0.95, 8),
                features=features,
                metadata={"trade_type": "funding_arb", "funding_rate": funding_rate},
            )

        return None

    def should_exit(self, symbol: str, df: pd.DataFrame, trade: dict) -> bool:
        price = float(df.iloc[-1]["close"])
        current_rate = trade.get("metadata", {}).get("funding_rate", 0)
        if abs(current_rate) < self._MIN_RATE * 0.3:
            return True
        if trade.get("stop_loss") and price <= float(trade["stop_loss"]):
            return True
        if trade.get("take_profit") and price >= float(trade["take_profit"]):
            return True
        return False
