import logging
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.metrics import accuracy_score, f1_score, precision_score, recall_score
from sqlalchemy import text

from data.db import get_session

logger = logging.getLogger(__name__)

# DB column names, positionally aligned with ml/predictor.FEATURE_ORDER.
SIGNAL_COLS = [
    "rsi", "macd", "macd_signal", "bb_upper", "bb_lower", "bb_position",
    "ema_9", "ema_21", "ema_50", "atr", "volume_ratio",
    "price_change_1h", "price_change_4h", "price_change_24h",
    "funding_rate", "orderbook_imbalance", "tv_recommendation",
]


class ModelTrainer:
    def __init__(self, config: dict):
        self.min_samples = config.get("min_trades_to_train", 50)
        self.model_dir = Path(config.get("model_path", "ml/models/"))
        self.model_dir.mkdir(parents=True, exist_ok=True)
        # A signal is a "win" only if it cleared a real net edge — not just
        # gross-positive. Defaults to ~0.15%, enough to beat typical round-trip
        # fees so the model never learns to chase sub-fee scratch wins.
        self.label_min_edge = config.get("label_min_edge", 0.0015)

    def train(self) -> dict | None:
        df = self._load_data()
        if df is None or len(df) < self.min_samples:
            logger.info(f"Insufficient training data: {0 if df is None else len(df)}/{self.min_samples}")
            return None

        # Chronological order (oldest -> newest) is essential for a leak-free
        # split: we must train on the past and validate on the future.
        df = df.sort_values("created_at").reset_index(drop=True)

        X = df[SIGNAL_COLS].fillna(0)
        # Fee-aware label: did the trade clear a meaningful net edge?
        pnl_pct = pd.to_numeric(df["actual_pnl_pct"], errors="coerce").fillna(0.0)
        y = (pnl_pct > self.label_min_edge).astype(int)

        if y.nunique() < 2:
            logger.info(
                f"Only one outcome class so far ({int(y.sum())} wins / {len(y)} "
                f"trades cleared {self.label_min_edge:.2%}). Need both to train."
            )
            return None

        # Chronological holdout: most-recent slice is the out-of-sample future.
        # Cap the test window at 200 so big histories still validate on recent
        # market conditions rather than a huge stale block.
        split = min(max(int(len(df) * 0.8), len(df) - 200), len(df) - 1)
        X_train, X_test = X.iloc[:split], X.iloc[split:]
        y_train, y_test = y.iloc[:split], y.iloc[split:]

        if y_train.nunique() < 2:
            logger.info("Training split has a single class — skipping this round.")
            return None

        # Recency weighting: newer trades carry more weight than stale ones.
        sample_w = np.linspace(0.6, 1.0, len(X_train))

        # Class imbalance: tell XGBoost how lopsided wins vs losses are.
        pos = int(y_train.sum())
        neg = len(y_train) - pos
        scale_pos_weight = (neg / pos) if pos else 1.0

        # Scale model capacity to the data we actually have — avoids overfitting
        # a deep 200-tree model to a few dozen rows.
        n_estimators = int(min(300, max(60, len(X_train) * 3)))
        max_depth = 3 if len(X_train) < 150 else (4 if len(X_train) < 400 else 5)

        # Early stopping only when the validation slice can support it.
        use_early_stop = len(X_test) >= 15 and y_test.nunique() == 2

        params = dict(
            n_estimators=n_estimators, max_depth=max_depth, learning_rate=0.05,
            subsample=0.85, colsample_bytree=0.85,
            reg_lambda=1.5, reg_alpha=0.5, min_child_weight=2,
            scale_pos_weight=scale_pos_weight,
            eval_metric="logloss", random_state=42,
        )
        if use_early_stop:
            params["early_stopping_rounds"] = 30

        model = xgb.XGBClassifier(**params)
        fit_kwargs = {"sample_weight": sample_w, "verbose": False}
        if use_early_stop:
            fit_kwargs["eval_set"] = [(X_test, y_test)]
        model.fit(X_train, y_train, **fit_kwargs)

        y_pred = model.predict(X_test)
        metrics = {
            "accuracy": float(accuracy_score(y_test, y_pred)),
            "precision": float(precision_score(y_test, y_pred, zero_division=0)),
            "recall": float(recall_score(y_test, y_pred, zero_division=0)),
            "f1": float(f1_score(y_test, y_pred, zero_division=0)),
            "training_samples": len(df),
        }
        logger.info(
            f"Model trained — acc={metrics['accuracy']:.3f} f1={metrics['f1']:.3f} "
            f"| {pos}/{len(y_train)} train-wins, spw={scale_pos_weight:.2f}, "
            f"trees={model.n_estimators}, depth={max_depth}, test_n={len(y_test)}"
        )

        version = self._next_version()
        model_path = self.model_dir / f"xgb_v{version}.pkl"
        joblib.dump(model, model_path)
        self._save_record(version, metrics, str(model_path))

        return {"version": version, "metrics": metrics, "model_file": str(model_path)}

    def _load_data(self) -> pd.DataFrame | None:
        session = get_session()
        try:
            cols = ", ".join(SIGNAL_COLS)
            result = session.execute(text(f"""
                SELECT {cols}, outcome, actual_pnl_pct, created_at FROM signals
                WHERE outcome IS NOT NULL
                ORDER BY created_at DESC LIMIT 10000
            """))
            rows = result.fetchall()
            if not rows:
                return None
            return pd.DataFrame(
                rows, columns=SIGNAL_COLS + ["outcome", "actual_pnl_pct", "created_at"]
            )
        finally:
            session.close()

    def _next_version(self) -> int:
        session = get_session()
        try:
            row = session.execute(text(
                "SELECT COALESCE(MAX(version), 0) FROM ml_models WHERE model_name='xgb_main'"
            )).fetchone()
            return (row[0] or 0) + 1
        finally:
            session.close()

    def _save_record(self, version: int, metrics: dict, path: str):
        session = get_session()
        try:
            session.execute(text("UPDATE ml_models SET is_active=0 WHERE model_name='xgb_main'"))
            session.execute(text("""
                INSERT INTO ml_models
                    (model_name, version, accuracy, precision_score, recall_score,
                     f1_score, training_samples, features_used, is_active, model_path)
                VALUES
                    ('xgb_main', :ver, :acc, :prec, :rec, :f1, :samples, :feats, 1, :path)
            """), {
                "ver": version, "acc": metrics["accuracy"], "prec": metrics["precision"],
                "rec": metrics["recall"], "f1": metrics["f1"],
                "samples": metrics["training_samples"], "feats": str(SIGNAL_COLS), "path": path,
            })
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to save model record: {e}")
        finally:
            session.close()
