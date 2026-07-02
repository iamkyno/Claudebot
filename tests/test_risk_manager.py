"""Money-path unit tests: position sizing, Kelly, ML scaling, trailing stops."""

from risk.manager import RiskManager

CFG = {
    "max_portfolio_risk_pct": 0.02,
    "cash_reserve_pct": 0.20,
    "atr_stop_multiplier": 1.5,
    "use_kelly_sizing": True,
    "kelly_fraction": 0.5,
}


def _rm(**over):
    return RiskManager({**CFG, **over})


class TestPositionSize:
    def test_basic_atr_sizing(self):
        rm = _rm(use_kelly_sizing=False)
        # risk $200 (2% of 10k), stop 3 away from 100 → 66.66 units → $6666,
        # capped at investable*0.25 = 8000*0.25 = 2000.
        size = rm.calculate_position_size(10_000, 100, atr=2.0, stop_loss=97)
        assert size == 2000.0

    def test_zero_risk_per_unit_rejected(self):
        rm = _rm()
        assert rm.calculate_position_size(10_000, 100, atr=0.0, stop_loss=None) == 0.0

    def test_kelly_scales_with_confidence(self):
        rm = _rm()
        hi = rm.calculate_position_size(10_000, 100, 2.0, 97, win_prob=0.80, reward_risk=2.0)
        lo = rm.calculate_position_size(10_000, 100, 2.0, 97, win_prob=0.62, reward_risk=2.0)
        assert hi > lo > 0

    def test_kelly_never_exceeds_no_kelly_cap(self):
        # Kelly can only shrink the risk budget, never exceed the hard cap.
        plain = _rm(use_kelly_sizing=False).calculate_position_size(10_000, 100, 2.0, 97)
        kelly = _rm().calculate_position_size(10_000, 100, 2.0, 97, win_prob=0.99, reward_risk=5.0)
        assert kelly <= plain

    def test_negative_edge_gets_floor_size(self):
        rm = _rm()
        # win_prob 0.4 with rr 1.0 → negative edge → floor (25% budget), not zero
        size = rm.calculate_position_size(10_000, 100, 2.0, 97, win_prob=0.40, reward_risk=1.0)
        assert size > 0


class TestMLConfidenceScaling:
    def test_rejects_below_threshold(self):
        assert _rm().adjust_for_ml_confidence(1000, 0.55) == 0.0

    def test_never_scales_above_one(self):
        # Cap at 1.0x: ML may only shrink a risk-managed size, never grow it.
        assert _rm().adjust_for_ml_confidence(1000, 0.99) <= 1000
        assert _rm().adjust_for_ml_confidence(1000, 1.00) == 1000

    def test_half_size_at_threshold(self):
        assert _rm().adjust_for_ml_confidence(1000, 0.60) == 500


class TestTrailingStop:
    def test_long_trails_below(self):
        rm = _rm()
        assert rm.trailing_stop("buy", 110.0, atr=2.0) == 107.0

    def test_short_trails_above(self):
        rm = _rm()
        assert rm.trailing_stop("sell", 90.0, atr=2.0) == 93.0


class TestBracketPrices:
    def test_long_stop_below_tp_above(self):
        rm = _rm()
        assert rm.stop_loss_price(100, 2.0, "buy") < 100 < rm.take_profit_price(100, 2.0, "buy")

    def test_short_stop_above_tp_below(self):
        rm = _rm()
        assert rm.stop_loss_price(100, 2.0, "sell") > 100 > rm.take_profit_price(100, 2.0, "sell")
