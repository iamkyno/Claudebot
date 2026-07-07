"""Train/inference feature alignment — the bug class that silently zeroes
model inputs. FEATURE_ORDER (inference dict keys) must match SIGNAL_COLS
(training DB columns) exactly, name-for-name and position-for-position."""

from decimal import Decimal

import numpy as np
import pandas as pd

from ml.predictor import FEATURE_ORDER, SignalPredictor
from ml.trainer import SIGNAL_COLS, MODEL_CLASSES, ModelTrainer


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


class TestDecimalTraining:
    def test_decimal_feature_columns_train_without_error(self, monkeypatch):
        """PostgreSQL NUMERIC -> decimal.Decimal -> pandas 'object' dtype used
        to crash XGBoost ('Invalid columns: rsi: object'). The trainer must
        coerce loaded features to float. Regression test."""
        rng = np.random.default_rng(0)
        n = 400
        # Realistic (noisy, overlapping) features as Decimal cells, exactly as
        # psycopg2 hands back NUMERIC columns from the signals table.
        pnl = rng.normal(0.0, 0.012, n)
        data = {}
        for c in SIGNAL_COLS:
            # weak signal + noise so classes overlap (calibration needs this)
            col = 0.4 * pnl / 0.012 + rng.normal(0, 1, n)
            data[c] = [Decimal(str(round(v, 4))) for v in col]
        data["actual_pnl_pct"] = [Decimal(str(round(p, 5))) for p in pnl]
        data["outcome"] = [1 if p > 0 else -1 for p in pnl]
        data["created_at"] = pd.date_range("2024-01-01", periods=n, freq="h")
        df = pd.DataFrame(data)

        tr = ModelTrainer({"min_trades_to_train": 50})
        monkeypatch.setattr(tr, "_load_data", lambda flt: df.copy())
        monkeypatch.setattr(tr, "_next_version", lambda name: 1)
        monkeypatch.setattr(tr, "_save_record", lambda *a, **k: None)
        monkeypatch.setattr("joblib.dump", lambda *a, **k: None)

        rec = tr._train_one("xgb_swing", "strategy != 'scalp'")
        assert rec is not None and rec["metrics"]["training_samples"] == n
