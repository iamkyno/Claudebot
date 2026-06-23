import logging
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.metrics import accuracy_score, f1_score, precision_score, recall_score
from sklearn.model_selection import train_test_split
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

    def train(self) -> dict | None:
        df = self._load_data()
        if df is None or len(df) < self.min_samples:
            logger.info(f"Insufficient training data: {0 if df is None else len(df)}/{self.min_samples}")
            return None

        X = df[SIGNAL_COLS].fillna(0)
        y = (df["outcome"] == 1).astype(int)

        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.20, random_state=42)

        model = xgb.XGBClassifier(
            n_estimators=200, max_depth=5, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8,
            eval_metric="logloss", random_state=42,
        )
        model.fit(X_train, y_train, eval_set=[(X_test, y_test)], verbose=False)

        y_pred = model.predict(X_test)
        metrics = {
            "accuracy": float(accuracy_score(y_test, y_pred)),
            "precision": float(precision_score(y_test, y_pred, zero_division=0)),
            "recall": float(recall_score(y_test, y_pred, zero_division=0)),
            "f1": float(f1_score(y_test, y_pred, zero_division=0)),
            "training_samples": len(df),
        }
        logger.info(f"Model trained — accuracy={metrics['accuracy']:.3f} f1={metrics['f1']:.3f}")

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
                SELECT {cols}, outcome FROM signals
                WHERE outcome IS NOT NULL
                ORDER BY created_at DESC LIMIT 10000
            """))
            rows = result.fetchall()
            if not rows:
                return None
            return pd.DataFrame(rows, columns=SIGNAL_COLS + ["outcome"])
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
