import logging
from datetime import datetime, timedelta
from typing import Optional
import pandas as pd
from sqlalchemy import text
from strategies.base import BaseStrategy, Signal
from data.db import get_session

logger = logging.getLogger(__name__)


class LiquidationCascadeStrategy(BaseStrategy):
    """
    Trades mean-reversion after large forced liquidation cascades.
    Liquidations create wicks that typically snap back violently.
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.name = "liquidation_cascade"
        self.min_usd = config.get("min_liquidation_usd", 5_000_000)
        self.lookback_secs = config.get("lookback_seconds", 60)

    def generate_signal(self, symbol: str, df: pd.DataFrame, **kwargs) -> Optional[Signal]:
        events = self._recent_liquidations(symbol)
        if not events:
            return None

        total_usd = sum(e["usd_value"] for e in events)
        if total_usd < self.min_usd:
            return None

        long_liq = sum(e["usd_value"] for e in events if e["side"] == "long")
        short_liq = sum(e["usd_value"] for e in events if e["side"] == "short")

        price = float(df.iloc[-1]["close"])
        atr = float(df.iloc[-1]["atr"]) if pd.notna(df.iloc[-1].get("atr")) else price * 0.01

        features = {
            "total_liquidation_usd": total_usd,
            "long_liquidations": long_liq,
            "short_liquidations": short_liq,
        }

        conf = min(0.60 + total_usd / 50_000_000, 0.92)

        if long_liq > short_liq and long_liq >= self.min_usd:
            # Long cascade → oversold wick → mean-revert upward
            return Signal(
                symbol=symbol, strategy=self.name, signal_type="buy",
                confidence=conf,
                stop_loss=round(price - atr * 1.5, 8),
                take_profit=round(price + atr * 2.5, 8),
                features=features,
                metadata={"total_liq_usd": total_usd},
            )

        if short_liq > long_liq and short_liq >= self.min_usd:
            # Short cascade → overbought wick → mean-revert downward
            return Signal(
                symbol=symbol, strategy=self.name, signal_type="sell",
                confidence=conf,
                stop_loss=round(price + atr * 1.5, 8),
                take_profit=round(price - atr * 2.5, 8),
                features=features,
                metadata={"total_liq_usd": total_usd},
            )

        return None

    def should_exit(self, symbol: str, df: pd.DataFrame, trade: dict) -> bool:
        price = float(df.iloc[-1]["close"])
        if trade.get("stop_loss") and price <= float(trade["stop_loss"]):
            return True
        if trade.get("take_profit") and price >= float(trade["take_profit"]):
            return True
        return False

    def _recent_liquidations(self, symbol: str) -> list:
        session = get_session()
        try:
            cutoff = datetime.utcnow() - timedelta(seconds=self.lookback_secs)
            result = session.execute(text("""
                SELECT side, usd_value FROM liquidation_events
                WHERE symbol=:symbol AND event_time >= :cutoff AND traded_on=0
            """), {"symbol": symbol, "cutoff": cutoff})
            return [{"side": r[0], "usd_value": float(r[1])} for r in result.fetchall()]
        finally:
            session.close()
