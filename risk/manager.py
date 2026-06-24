import logging

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, config: dict):
        self.max_risk_pct = config.get("max_portfolio_risk_pct", 0.02)
        self.atr_stop_mult = config.get("atr_stop_multiplier", 1.5)
        self.max_concurrent = config.get("max_concurrent_positions", 8)
        self.cash_reserve = config.get("cash_reserve_pct", 0.20)
        # Kelly sizing: fraction of full-Kelly to actually bet (half-Kelly is
        # the standard, far smoother than full-Kelly which is very swingy).
        self.use_kelly = config.get("use_kelly_sizing", True)
        self.kelly_fraction = config.get("kelly_fraction", 0.5)

    def calculate_position_size(
        self, balance: float, price: float, atr: float, stop_loss: float = None,
        win_prob: float = None, reward_risk: float = 2.0,
    ) -> float:
        """
        Position sizing. Always respects the max_risk_pct hard cap; when an ML
        win-probability is supplied, the Kelly criterion sets how much of that
        risk budget to actually deploy (sizing up only when the edge is real).
        """
        investable = balance * (1 - self.cash_reserve)
        risk_dollars = balance * self.max_risk_pct

        if stop_loss and stop_loss > 0:
            risk_per_unit = abs(price - stop_loss)
        else:
            risk_per_unit = atr * self.atr_stop_mult

        if risk_per_unit <= 0:
            return 0.0

        # Kelly scales the risk budget by edge quality. f* = p - (1-p)/b.
        if self.use_kelly and win_prob is not None and reward_risk > 0:
            edge = win_prob - (1 - win_prob) / reward_risk
            kelly = max(0.0, min(edge * self.kelly_fraction, 1.0))
            # Map full risk budget to Kelly; floor at 25% so a marginal-but-
            # accepted trade still takes a small position rather than ~0.
            risk_dollars *= max(kelly, 0.25) if edge > 0 else 0.25

        units = risk_dollars / risk_per_unit
        position_value = min(units * price, investable * 0.25)
        return round(position_value, 2)

    def trailing_stop(self, side: str, extreme_price: float, atr: float) -> float:
        """
        New stop level trailing the best price seen. For a long it sits
        atr*mult BELOW the highest price; for a short, above the lowest.
        Caller only moves the stop in the favourable direction.
        """
        offset = atr * self.atr_stop_mult
        if side == "buy":
            return round(extreme_price - offset, 8)
        return round(extreme_price + offset, 8)

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
