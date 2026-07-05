import logging
from typing import Optional
import pandas as pd
from strategies.base import BaseStrategy, Signal

logger = logging.getLogger(__name__)


class GridStrategy(BaseStrategy):
    """
    Grid trading with ATR-derived spacing.
    Grid level spacing = 0.5 × (ATR / price), clamped to 0.2–1.5%.
    Automatically killed in trending markets (ADX > 35).
    """

    _LEVELS = 12

    def __init__(self, config: dict):
        super().__init__(config)
        self.name = "grid"
        self._grids: dict = {}
        # Tunable geometry — overridden by config/tuning.json when the
        # optimizer has found better values on historical data.
        self.spacing_mult = float(config.get("spacing_mult", 0.5))   # x ATR/price
        self.stop_mult = float(config.get("stop_mult", 4.0))         # x spacing
        self.tp_mult = float(config.get("tp_mult", 1.0))             # x spacing
        self.adx_kill = float(config.get("adx_kill", 35))

    def generate_signal(self, symbol: str, df: pd.DataFrame, **kwargs) -> Optional[Signal]:
        if len(df) < 50:
            return None

        last = df.iloc[-1]
        price = float(last["close"])
        adx = float(last["adx"]) if pd.notna(last.get("adx")) else 0.0
        atr = float(last["atr"]) if pd.notna(last.get("atr")) else price * 0.01

        if adx > self.adx_kill:
            self._grids.pop(symbol, None)
            return None

        spacing = self._calc_spacing(price, atr, self.spacing_mult)
        grid = self._grids.get(symbol)

        # Rebuild grid if price has drifted more than 3× spacing from center
        if grid and abs(price - grid["center"]) / price > spacing * 3:
            del self._grids[symbol]
            grid = None

        grid = grid or self._make_grid(symbol, price, spacing)

        features = {
            "grid_center": grid["center"], "current_price": price,
            "adx": adx, "spacing_pct": grid["spacing"],
        }

        unfilled_buys = [lvl for lvl in grid["levels"]
                         if lvl < price and not grid["filled"].get(lvl)]
        if unfilled_buys:
            nearest = max(unfilled_buys)
            if abs(price - nearest) / price < spacing * 0.5:
                grid["filled"][nearest] = True
                return Signal(
                    symbol=symbol, strategy=self.name, signal_type="buy",
                    confidence=0.55,
                    stop_loss=round(price * (1 - spacing * self.stop_mult), 8),
                    take_profit=round(nearest * (1 + spacing * self.tp_mult), 8),
                    features=features,
                )
        return None

    def should_exit(self, symbol: str, df: pd.DataFrame, trade: dict) -> bool:
        last = df.iloc[-1]
        price = float(last["close"])
        adx = float(last["adx"]) if pd.notna(last.get("adx")) else 0.0

        if adx > self.adx_kill:
            self._grids.pop(symbol, None)
            return True
        if trade.get("stop_loss") and price <= float(trade["stop_loss"]):
            return True
        if trade.get("take_profit") and price >= float(trade["take_profit"]):
            return True
        return False

    # ------------------------------------------------------------------ #

    @staticmethod
    def _calc_spacing(price: float, atr: float, mult: float = 0.5) -> float:
        raw = (atr / price) * mult
        return max(0.002, min(raw, 0.015))  # clamp 0.2% – 1.5%

    def _make_grid(self, symbol: str, center: float, spacing: float) -> dict:
        half = self._LEVELS // 2
        lvls = sorted([
            round(center * (1 + spacing * i), 8)
            for i in range(-half, half + 1) if i != 0
        ])
        grid = {"center": center, "spacing": spacing, "levels": lvls, "filled": {}}
        self._grids[symbol] = grid
        logger.info(f"Grid {symbol}: center={center:.4f} spacing={spacing:.3%} levels={len(lvls)}")
        return grid
