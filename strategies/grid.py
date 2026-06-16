import logging
from typing import Optional
import pandas as pd
from strategies.base import BaseStrategy, Signal

logger = logging.getLogger(__name__)


class GridStrategy(BaseStrategy):
    """
    Grid trading: profit from oscillation by placing orders at fixed intervals.
    Automatically disables itself when ADX signals a strong directional trend.
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.name = "grid"
        self.spacing = config.get("grid_spacing_pct", 0.003)
        self.levels = config.get("grid_levels", 10)
        self._grids: dict = {}

    def generate_signal(self, symbol: str, df: pd.DataFrame, **kwargs) -> Optional[Signal]:
        if len(df) < 50:
            return None

        last = df.iloc[-1]
        price = float(last["close"])
        adx = float(last["adx"]) if pd.notna(last.get("adx")) else 0.0

        # Kill grid in trending markets
        if adx > 35:
            self._grids.pop(symbol, None)
            return None

        grid = self._grids.get(symbol) or self._make_grid(symbol, price)

        features = {
            "grid_center": grid["center"], "current_price": price,
            "adx": adx, "spacing_pct": self.spacing,
        }

        unfilled_buys = [lvl for lvl in grid["levels"] if lvl < price and not grid["filled"].get(lvl)]
        if unfilled_buys:
            nearest = max(unfilled_buys)
            if abs(price - nearest) / price < self.spacing * 0.5:
                grid["filled"][nearest] = True
                return Signal(
                    symbol=symbol, strategy=self.name, signal_type="buy",
                    confidence=0.55,
                    stop_loss=round(price * (1 - self.spacing * 4), 8),
                    take_profit=round(nearest * (1 + self.spacing), 8),
                    features=features,
                )
        return None

    def should_exit(self, symbol: str, df: pd.DataFrame, trade: dict) -> bool:
        last = df.iloc[-1]
        price = float(last["close"])
        adx = float(last["adx"]) if pd.notna(last.get("adx")) else 0.0

        if adx > 35:
            self._grids.pop(symbol, None)
            return True
        if trade.get("stop_loss") and price <= float(trade["stop_loss"]):
            return True
        if trade.get("take_profit") and price >= float(trade["take_profit"]):
            return True
        return False

    def _make_grid(self, symbol: str, center: float) -> dict:
        half = self.levels // 2
        lvls = sorted([
            round(center * (1 + self.spacing * i), 8)
            for i in range(-half, half + 1) if i != 0
        ])
        grid = {"center": center, "levels": lvls, "filled": {}}
        self._grids[symbol] = grid
        logger.info(f"Grid created for {symbol} around {center:.4f}, {len(lvls)} levels")
        return grid
