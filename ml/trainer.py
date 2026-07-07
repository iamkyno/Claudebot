import logging
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import (
    accuracy_score, brier_score_loss, f1_score, precision_score, recall_score,
)
from sklearn.model_selection import TimeSeriesSplit
from sqlalchemy import text

from data.db import get_session

logger = logging.getLogger(__name__)

# DB column names, positionally aligned with ml/predictor.FEATURE_ORDER.
# New features are APPENDED only — old signal rows read NULL -> 0 for them,
# so historical data keeps training alongside the richer new rows.
SIGNAL_COLS = [
    "rsi", "macd", "macd_signal", "bb_upper", "bb_lower", "bb_position",
    "ema_9", "ema_21", "ema_50", "atr", "volume_ratio",
    "price_change_1h", "price_change_4h", "price_change_24h",
    "funding_rate", "orderbook_imbalance", "tv_recommendation",
    "adx", "bb_width", "oi_change", "taker_flow",
]

# 1m scalp signals and 1h swing signals are different populations (features,
# horizons, fee profiles). Each class gets its own model + its own bootstrap.
MODEL_CLASSES = {
    "xgb_scalp": "strategy = 'scalp'",
    "xgb_swing": "strategy != 'scalp'",
}


class ModelTrainer:
    def __init__(self, config: dict):
        self.min_samples = config.get("min_trades_to_train", 50)
        self.model_dir = Path(config.get("model_path", "ml/models/"))
        self.model_dir.mkdir(parents=True, exist_ok=True)
        # A signal is a "win" only if it cleared a real net edge. PnL is now
        # net of fees+slippage at the source, so this is a pure quality bar.
        self.label_min_edge = config.get("label_min_edge", 0.0015)

    def train(self) -> list[dict]:
        """Train one calibrated model per strategy class. Returns records for
        every model that actually trained (possibly an empty list)."""
        results = []
        for model_name, class_filter in MODEL_CLASSES.items():
            try:
                rec = self._train_one(model_name, class_filter)
                if rec:
                    results.append(rec)
            except Exception as e:
                logger.error(f"Training {model_name} failed: {e}")
        return results

    def _train_one(self, model_name: str, class_filter: str) -> dict | None:
        df = self._load_data(class_filter)
        if df is None or len(df) < self.min_samples:
            logger.info(
                f"{model_name}: insufficient data "
                f"({0 if df is None else len(df)}/{self.min_samples})"
            )
            return None

        # Chronological order (oldest -> newest) is essential for a leak-free
        # split: we must train on the past and validate on the future.
        df = df.sort_values("created_at").reset_index(drop=True)

        # Coerce at the point of use: PostgreSQL NUMERIC arrives as
        # decimal.Decimal (pandas 'object' dtype) which XGBoost rejects.
        # Belt-and-suspenders with _load_data's coercion — guarantees float
        # features no matter how the frame reached here.
        X = df[SIGNAL_COLS].apply(pd.to_numeric, errors="coerce").fillna(0)
        # Fee-aware label: did the trade clear a meaningful net edge?
        pnl_pct = pd.to_numeric(df["actual_pnl_pct"], errors="coerce").fillna(0.0)
        y = (pnl_pct > self.label_min_edge).astype(int)

        if y.nunique() < 2:
            logger.info(f"{model_name}: only one outcome class so far — skipping.")
            return None

        # Chronological holdout: most-recent slice is the out-of-sample future.
        split = min(max(int(len(df) * 0.8), len(df) - 200), len(df) - 1)
        X_train, X_test = X.iloc[:split], X.iloc[split:]
        y_train, y_test = y.iloc[:split], y.iloc[split:]

        if y_train.nunique() < 2:
            logger.info(f"{model_name}: training split single-class — skipping.")
            return None

        # Recency weighting: newer trades carry more weight than stale ones.
        sample_w = np.linspace(0.6, 1.0, len(X_train))

        pos = int(y_train.sum())
        neg = len(y_train) - pos
        scale_pos_weight = (neg / pos) if pos else 1.0

        n_estimators = int(min(300, max(60, len(X_train) * 3)))
        max_depth = 3 if len(X_train) < 150 else (4 if len(X_train) < 400 else 5)

        base = xgb.XGBClassifier(
            n_estimators=n_estimators, max_depth=max_depth, learning_rate=0.05,
            subsample=0.85, colsample_bytree=0.85,
            reg_lambda=1.5, reg_alpha=0.5, min_child_weight=2,
            scale_pos_weight=scale_pos_weight,
            eval_metric="logloss", random_state=42,
        )

        # Calibration: raw XGBoost predict_proba is NOT a probability — and
        # Kelly sizing is exactly the formula that punishes overconfidence.
        # Isotonic needs a few hundred samples to avoid overfitting the
        # calibration curve itself; below that, Platt (sigmoid) is safer.
        method = "isotonic" if len(X_train) >= 300 else "sigmoid"
        model = CalibratedClassifierCV(base, method=method, cv=3)
        model.fit(X_train, y_train, sample_weight=sample_w)

        y_pred = model.predict(X_test)
        y_prob = model.predict_proba(X_test)[:, 1]
        metrics = {
            "accuracy": float(accuracy_score(y_test, y_pred)),
            "precision": float(precision_score(y_test, y_pred, zero_division=0)),
            "recall": float(recall_score(y_test, y_pred, zero_division=0)),
            "f1": float(f1_score(y_test, y_pred, zero_division=0)),
            "brier": float(brier_score_loss(y_test, y_prob)),
            "training_samples": len(df),
        }

        # Walk-forward CV: a single holdout can flatter or damn a model by
        # luck of the window — 3 expanding time folds give an honest spread.
        cv_f1 = self._walk_forward_f1(X, y, sample_len=len(df))
        if cv_f1 is not None:
            metrics["cv_f1"] = cv_f1

        logger.info(
            f"{model_name} trained — acc={metrics['accuracy']:.3f} "
            f"f1={metrics['f1']:.3f} brier={metrics['brier']:.3f}"
            + (f" cv_f1={cv_f1:.3f}" if cv_f1 is not None else "")
            + f" ({method}-calibrated, {pos}/{len(y_train)} train-wins, "
            f"test_n={len(y_test)})"
        )

        version = self._next_version(model_name)
        model_path = self.model_dir / f"{model_name}_v{version}.pkl"
        joblib.dump(model, model_path)
        self._save_record(model_name, version, metrics, str(model_path))

        return {"model_name": model_name, "version": version,
                "metrics": metrics, "model_file": str(model_path)}

    @staticmethod
    def _walk_forward_f1(X, y, sample_len: int) -> float | None:
        """Mean F1 across 3 expanding time folds (plain XGB, uncalibrated —
        this is a stability read on the signal, not the shipped model)."""
        if sample_len < 90:
            return None
        try:
            scores = []
            for tr_idx, te_idx in TimeSeriesSplit(n_splits=3).split(X):
                y_tr, y_te = y.iloc[tr_idx], y.iloc[te_idx]
                if y_tr.nunique() < 2 or y_te.nunique() < 2:
                    continue
                m = xgb.XGBClassifier(
                    n_estimators=80, max_depth=3, learning_rate=0.05,
                    subsample=0.85, colsample_bytree=0.85,
                    reg_lambda=1.5, eval_metric="logloss", random_state=42,
                )
                m.fit(X.iloc[tr_idx], y_tr)
                scores.append(f1_score(y_te, m.predict(X.iloc[te_idx]), zero_division=0))
            return float(np.mean(scores)) if scores else None
        except Exception:
            return None

    def _load_data(self, class_filter: str) -> pd.DataFrame | None:
        session = get_session()
        try:
            cols = ", ".join(SIGNAL_COLS)
            result = session.execute(text(f"""
                SELECT {cols}, outcome, actual_pnl_pct, created_at FROM signals
                WHERE outcome IS NOT NULL AND {class_filter}
                ORDER BY created_at DESC LIMIT 10000
            """))
            rows = result.fetchall()
            if not rows:
                return None
            df = pd.DataFrame(
                rows, columns=SIGNAL_COLS + ["outcome", "actual_pnl_pct", "created_at"]
            )
            # PostgreSQL NUMERIC comes back as decimal.Decimal -> pandas 'object'
            # dtype, which XGBoost rejects ("Invalid columns: rsi: object, …").
            # Coerce every feature + label column to real floats.
            df[SIGNAL_COLS] = df[SIGNAL_COLS].apply(pd.to_numeric, errors="coerce")
            df["actual_pnl_pct"] = pd.to_numeric(df["actual_pnl_pct"], errors="coerce")
            return df
        finally:
            session.close()

    def _next_version(self, model_name: str) -> int:
        session = get_session()
        try:
            row = session.execute(text(
                "SELECT COALESCE(MAX(version), 0) FROM ml_models WHERE model_name=:m"
            ), {"m": model_name}).fetchone()
            return (row[0] or 0) + 1
        finally:
            session.close()

    def _save_record(self, model_name: str, version: int, metrics: dict, path: str):
        session = get_session()
        try:
            session.execute(text(
                "UPDATE ml_models SET is_active=0 WHERE model_name=:m"
            ), {"m": model_name})
            session.execute(text("""
                INSERT INTO ml_models
                    (model_name, version, accuracy, precision_score, recall_score,
                     f1_score, training_samples, features_used, is_active, model_path)
                VALUES
                    (:m, :ver, :acc, :prec, :rec, :f1, :samples, :feats, 1, :path)
            """), {
                "m": model_name, "ver": version, "acc": metrics["accuracy"],
                "prec": metrics["precision"], "rec": metrics["recall"],
                "f1": metrics["f1"], "samples": metrics["training_samples"],
                "feats": str(SIGNAL_COLS), "path": path,
            })
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to save model record: {e}")
        finally:
            session.close()
