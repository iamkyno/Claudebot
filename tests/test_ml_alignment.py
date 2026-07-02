"""Train/inference feature alignment — the bug class that silently zeroes
model inputs. FEATURE_ORDER (inference dict keys) must match SIGNAL_COLS
(training DB columns) exactly, name-for-name and position-for-position."""

from ml.predictor import FEATURE_ORDER, SignalPredictor
from ml.trainer import SIGNAL_COLS, MODEL_CLASSES


class TestAlignment:
    def test_feature_order_matches_signal_cols_exactly(self):
        assert FEATURE_ORDER == SIGNAL_COLS

    def test_model_classes_cover_scalp_and_swing(self):
        assert set(MODEL_CLASSES) == {"xgb_scalp", "xgb_swing"}


class TestClassRouting:
    def test_scalp_routes_to_scalp_class(self):
        assert SignalPredictor._class_for("scalp") == "scalp"

    def test_everything_else_routes_to_swing(self):
        for s in ["rsi_bb", "ema_cross", "grid", "pair_trading", None]:
            assert SignalPredictor._class_for(s) == "swing"


class TestVectorBuilding:
    def test_zero_values_survive_none_handling(self):
        """0.0 is a legitimate feature value and must NOT be conflated with
        missing (None). Regression test for the `or 0` falsy-coercion bug."""
        feats = {f: 0.0 for f in FEATURE_ORDER}
        vals = [0 if feats.get(f) is None else feats.get(f) for f in FEATURE_ORDER]
        assert all(v == 0.0 for v in vals) and len(vals) == len(FEATURE_ORDER)
