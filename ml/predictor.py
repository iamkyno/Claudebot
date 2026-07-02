import logging
from pathlib import Path
from typing import Tuple

import joblib
import numpy as np
from sqlalchemy import text

from data.db import get_session

logger = logging.getLogger(__name__)

# Keys here must read from the in-memory feature dict produced by the strategies
# and data/features.py. They map POSITIONALLY to the DB columns in
# ml/trainer.SIGNAL_COLS, so order + length must stay in sync with that list.
FEATURE_ORDER = [
    "rsi", "macd", "macd_signal", "bb_upper", "bb_lower", "bb_position",
    "ema_9", "ema_21", "ema_50", "atr", "volume_ratio",
    "price_change_1h", "price_change_4h", "price_change_24h",
    "funding_rate", "orderbook_imbalance", "tv_recommendation",
]


class SignalPredictor:
    """
    Two calibrated models, one per strategy class: 1m scalp signals and 1h
    swing signals are different populations, so they never share a model.
    Each class bootstraps independently (no model yet -> trade & collect data).
    """

    def __init__(self, config: dict):
        self.threshold = config.get("confidence_threshold", 0.60)
        self.model_dir = Path(config.get("model_path", "ml/models/"))
        self._models: dict[str, object] = {}     # class -> model
        self._versions: dict[str, int] = {}
        self._load_attempted = False

    @staticmethod
    def _class_for(strategy: str | None) -> str:
        return "scalp" if strategy == "scalp" else "swing"

    def load_model(self) -> bool:
        """Load the active model for each class. 'xgb_main' is accepted as a
        legacy fallback for the swing class so old installs keep their model."""
        session = get_session()
        loaded = False
        try:
            for cls, names in (("scalp", ["xgb_scalp"]),
                               ("swing", ["xgb_swing", "xgb_main"])):
                for name in names:
                    row = session.execute(text("""
                        SELECT version, model_path FROM ml_models
                        WHERE model_name=:m AND is_active=1
                        ORDER BY trained_at DESC LIMIT 1
                    """), {"m": name}).fetchone()
                    if not row:
                        continue
                    try:
                        self._models[cls] = joblib.load(row[1])
                        self._versions[cls] = row[0]
                        logger.info(f"Loaded ML model {name} v{row[0]} ({cls})")
                        loaded = True
                        break
                    except Exception as e:
                        logger.warning(f"Could not load {name}: {e}")
        finally:
            session.close()
        self._load_attempted = True
        return loaded

    def _model_for(self, strategy: str | None):
        if not self._load_attempted:
            self.load_model()
        return self._models.get(self._class_for(strategy))

    def score_signal(self, features: dict, strategy: str | None = None) -> float:
        """Calibrated win probability (0-1). Falls back to 0.5 (bootstrap)."""
        model = self._model_for(strategy)
        if model is None:
            return 0.5
        try:
            vals = [0 if features.get(f) is None else features.get(f) for f in FEATURE_ORDER]
            X = np.array(vals, dtype=float).reshape(1, -1)
            return float(model.predict_proba(X)[0][1])
        except Exception as e:
            logger.debug(f"Scoring failed: {e}")
            return 0.5

    def unload(self):
        """Drop loaded models (used after a paper reset deactivates them all).
        Both classes fall back to bootstrap mode until new models train."""
        self._models.clear()
        self._versions.clear()
        self._load_attempted = True

    @property
    def has_model(self) -> bool:
        return bool(self._models)

    def has_model_for(self, strategy: str | None) -> bool:
        return self._model_for(strategy) is not None

    def should_trade(self, features: dict, strategy: str | None = None) -> Tuple[bool, float]:
        conf = self.score_signal(features, strategy)
        # Bootstrap per class: no model yet for this class -> don't block; the
        # bot needs to place (and label) trades before it can learn from them.
        if not self.has_model_for(strategy):
            return True, conf
        return conf >= self.threshold, conf

    def log_signal(
        self, symbol: str, strategy: str, signal_type: str,
        confidence: float, features: dict,
    ) -> int | None:
        session = get_session()
        try:
            # Map feature keys to DB column names
            f = features
            result = session.execute(text("""
                INSERT INTO signals
                    (symbol, strategy, signal_type, confidence,
                     rsi, macd, macd_signal, bb_upper, bb_lower, bb_position,
                     ema_9, ema_21, ema_50, atr, volume_ratio,
                     price_change_1h, price_change_4h, price_change_24h,
                     funding_rate, orderbook_imbalance, tv_recommendation)
                VALUES
                    (:symbol, :strategy, :signal_type, :confidence,
                     :rsi, :macd, :macd_signal, :bb_upper, :bb_lower, :bb_position,
                     :ema_9, :ema_21, :ema_50, :atr, :volume_ratio,
                     :pc1h, :pc4h, :pc24h, :funding_rate, :ob_imbalance, :tv)
                RETURNING id
            """), {
                "symbol": symbol, "strategy": strategy,
                "signal_type": signal_type, "confidence": confidence,
                "rsi": f.get("rsi"), "macd": f.get("macd"),
                "macd_signal": f.get("macd_signal"), "bb_upper": f.get("bb_upper"),
                "bb_lower": f.get("bb_lower"), "bb_position": f.get("bb_position"),
                "ema_9": f.get("ema_9"), "ema_21": f.get("ema_21"), "ema_50": f.get("ema_50"),
                "atr": f.get("atr"), "volume_ratio": f.get("volume_ratio"),
                "pc1h": f.get("price_change_1h"), "pc4h": f.get("price_change_4h"),
                "pc24h": f.get("price_change_24h"),
                "funding_rate": f.get("funding_rate"),
                "ob_imbalance": f.get("orderbook_imbalance"),
                "tv": f.get("tv_recommendation"),
            })
            signal_id = result.scalar()
            session.commit()
            return signal_id
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to log signal: {e}")
            return None
        finally:
            session.close()
