import logging
import threading
import time
import pandas as pd
from datetime import datetime
from sqlalchemy import text
from data.db import get_session

logger = logging.getLogger(__name__)


class DataFetcher:
    def __init__(self, exchange_client):
        self.exchange = exchange_client
        # Short-lived in-memory cache so the sniper loop can re-read the same
        # candles every few seconds without re-hitting the network.
        self._mem: dict[tuple, tuple[float, pd.DataFrame]] = {}
        self._mem_lock = threading.Lock()

    def fetch_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 500,
                    mem_ttl: float = 0.0, cache_tail: int = 120) -> pd.DataFrame:
        key = (symbol, timeframe, limit)
        if mem_ttl > 0:
            with self._mem_lock:
                hit = self._mem.get(key)
            if hit and (time.time() - hit[0]) < mem_ttl:
                return hit[1]
        try:
            raw = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            df.set_index("timestamp", inplace=True)
            if mem_ttl > 0:
                with self._mem_lock:
                    self._mem[key] = (time.time(), df)
            self._cache(symbol, timeframe, df, tail=cache_tail)
            return df
        except Exception as e:
            logger.error(f"Failed to fetch OHLCV for {symbol}: {e}")
            return self._load_from_cache(symbol, timeframe, limit)

    def _cache(self, symbol: str, timeframe: str, df: pd.DataFrame, tail: int = 120):
        # Only upsert the most recent candles: caching 500 rows per symbol per
        # tick is ~30k writes/min across the universe for data that's already
        # there. The tail keeps the offline fallback deep enough while the
        # high-frequency scalp loop passes a much smaller tail (fresh bars only).
        df = df.tail(tail)
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
            df[["open","high","low","close","volume"]] = df[["open","high","low","close","volume"]].astype(float)
            df.set_index("timestamp", inplace=True)
            return df.sort_index()
        finally:
            session.close()
