import logging
import pandas as pd
from datetime import datetime
from sqlalchemy import text
from data.db import get_session

logger = logging.getLogger(__name__)


class DataFetcher:
    def __init__(self, exchange_client):
        self.exchange = exchange_client

    def fetch_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 500) -> pd.DataFrame:
        try:
            raw = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            df.set_index("timestamp", inplace=True)
            self._cache(symbol, timeframe, df)
            return df
        except Exception as e:
            logger.error(f"Failed to fetch OHLCV for {symbol}: {e}")
            return self._load_from_cache(symbol, timeframe, limit)

    def _cache(self, symbol: str, timeframe: str, df: pd.DataFrame):
        session = get_session()
        try:
            for ts, row in df.iterrows():
                session.execute(text("""
                    INSERT INTO ohlcv_cache
                        (symbol, timeframe, open_time, open_price, high_price, low_price, close_price, volume)
                    VALUES
                        (:symbol, :timeframe, :open_time, :open, :high, :low, :close, :volume)
                    ON CONFLICT (symbol, timeframe, open_time)
                    DO UPDATE SET close_price=EXCLUDED.close_price, volume=EXCLUDED.volume
                """), {
                    "symbol": symbol, "timeframe": timeframe, "open_time": ts,
                    "open": row["open"], "high": row["high"],
                    "low": row["low"], "close": row["close"], "volume": row["volume"],
                })
            session.commit()
        except Exception as e:
            session.rollback()
            logger.debug(f"Cache write skipped: {e}")
        finally:
            session.close()

    def _load_from_cache(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        session = get_session()
        try:
            result = session.execute(text("""
                SELECT open_time, open_price, high_price, low_price, close_price, volume
                FROM ohlcv_cache
                WHERE symbol=:symbol AND timeframe=:timeframe
                ORDER BY open_time DESC LIMIT :limit
            """), {"symbol": symbol, "timeframe": timeframe, "limit": limit})
            rows = result.fetchall()
            if not rows:
                return pd.DataFrame()
            df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df.set_index("timestamp", inplace=True)
            return df.sort_index()
        finally:
            session.close()
