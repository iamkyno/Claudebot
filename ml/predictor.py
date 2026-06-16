import logging
from pathlib import Path
from typing import Tuple

import joblib
import numpy as np
from sqlalchemy import text

from data.db import get_session

logger = logging.getLogger(__name__)

FEATURE_ORDER = [
    "rsi", "macd", "macd_signal", "bb_upper", "bb_lower", "bb_position",
    "ema_9", "ema_21", "ema_50", "atr", "volume_ratio",
    "price_change_1h", "price_change_4h", "price_change_24h",
    "funding_rate", "orderbook_imbalance",
]


class SignalPredictor:
    def __init__(self, config: dict):
        self.threshold = config.get("confidence_threshold", 0.60)
        self.model_dir = Path(config.get("model_path", "ml/models/"))
        self._model = None
        self._version = None

    def load_model(self) -> bool:
        session = get_session()
        try:
            row = session.execute(text("""
                SELECT version, model_path FROM ml_models
                WHERE model_name='xgb_main' AND is_active=1
                ORDER BY trained_at DESC LIMIT 1
            """)).fetchone()
            if not row:
                return False
            self._model = joblib.load(row[1])
            self._version = row[0]
            logger.info(f"Loaded ML model v{self._version}")
            return True
        except Exception as e:
            logger.warning(f"Could not load ML model: {e}")
            return False
        finally:
            session.close()

    def score_signal(self, features: dict) -> float:
        """Return probability of a profitable trade (0–1). Falls back to 0.5."""
        if self._model is None and not self.load_model():
            return 0.5
        try:
            vals = [features.get(f) or 0 for f in FEATURE_ORDER]
            X = np.array(vals, dtype=float).reshape(1, -1)
            return float(self._model.predict_proba(X)[0][1])
        except Exception as e:
            logger.debug(f"Scoring failed: {e}")
            return 0.5

    def should_trade(self, features: dict) -> Tuple[bool, float]:
        conf = self.score_signal(features)
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
                     funding_rate, orderbook_imbalance)
                VALUES
                    (:symbol, :strategy, :signal_type, :confidence,
                     :rsi, :macd, :macd_signal, :bb_upper, :bb_lower, :bb_position,
                     :ema_9, :ema_21, :ema_50, :atr, :volume_ratio,
                     :pc1h, :pc4h, :pc24h, :funding_rate, :ob_imbalance)
            """), {
                "symbol": symbol, "strategy": strategy,
                "signal_type": signal_type, "confidence": confidence,
                "rsi": f.get("rsi"), "macd": f.get("macd"),
                "macd_signal": f.get("macd_signal"), "bb_upper": f.get("bb_upper"),
                "bb_lower": f.get("bb_lower"), "bb_position": f.get("bb_position"),
                "ema_9": f.get("ema_9"), "ema_21": f.get("ema_21"), "ema_50": f.get("ema_50"),
                "atr": f.get("atr"), "volume_ratio": f.get("volume_ratio"),
                "pc1h": f.get("price_change_1"), "pc4h": f.get("price_change_4"),
                "pc24h": f.get("price_change_24"),
                "funding_rate": f.get("funding_rate"),
                "ob_imbalance": f.get("orderbook_imbalance"),
            })
            session.commit()
            return result.lastrowid
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to log signal: {e}")
            return None
        finally:
            session.close()
