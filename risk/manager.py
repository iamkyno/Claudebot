import logging

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, config: dict):
        self.max_risk_pct = config.get("max_portfolio_risk_pct", 0.02)
        self.atr_stop_mult = config.get("atr_stop_multiplier", 1.5)
        self.max_concurrent = config.get("max_concurrent_positions", 8)
        self.cash_reserve = config.get("cash_reserve_pct", 0.20)

    def calculate_position_size(
        self, balance: float, price: float, atr: float, stop_loss: float = None
    ) -> float:
        """ATR-based position sizing. Risks max_risk_pct of balance per trade."""
        investable = balance * (1 - self.cash_reserve)
        risk_dollars = balance * self.max_risk_pct

        if stop_loss and stop_loss > 0:
            risk_per_unit = abs(price - stop_loss)
        else:
            risk_per_unit = atr * self.atr_stop_mult

        if risk_per_unit <= 0:
            return 0.0

        units = risk_dollars / risk_per_unit
        position_value = min(units * price, investable * 0.25)
        return round(position_value, 2)

    def can_open_position(self, open_trades: list, strategy: str, balance: float) -> bool:
        if len(open_trades) >= self.max_concurrent:
            logger.debug(f"Max concurrent positions reached ({self.max_concurrent})")
            return False
        if balance <= 0:
            logger.warning("Zero balance, cannot open position")
            return False
        return True

    def adjust_for_ml_confidence(self, position_size: float, confidence: float) -> float:
        """Scale size 0.5x–1.0x based on ML confidence. Reject below threshold.
        Max is 1.0x so ML can only reduce size, never exceed the risk-managed cap."""
        if confidence < 0.60:
            return 0.0
        scale = min((confidence - 0.60) / 0.40 * 0.5 + 0.5, 1.0)
        return round(position_size * scale, 2)

    def stop_loss_price(self, price: float, atr: float, side: str = "buy") -> float:
        offset = atr * self.atr_stop_mult
        return round(price - offset if side == "buy" else price + offset, 8)

    def take_profit_price(self, price: float, atr: float, side: str = "buy", rr: float = 2.0) -> float:
        offset = atr * self.atr_stop_mult * rr
        return round(price + offset if side == "buy" else price - offset, 8)
