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
        # Only the most recent candles, and ONE multi-row upsert instead of a
        # Python loop of single INSERTs — cuts DB round-trips ~100x per fetch.
        df = df.tail(tail)
        if df.empty:
            return
        values, params = [], {"symbol": symbol, "timeframe": timeframe}
        for i, (ts, row) in enumerate(df.iterrows()):
            values.append(f"(:symbol, :timeframe, :t{i}, :o{i}, :h{i}, :l{i}, :c{i}, :v{i})")
            params.update({f"t{i}": ts, f"o{i}": row["open"], f"h{i}": row["high"],
                           f"l{i}": row["low"], f"c{i}": row["close"], f"v{i}": row["volume"]})
        sql = f"""
            INSERT INTO ohlcv_cache
                (symbol, timeframe, open_time, open_price, high_price, low_price, close_price, volume)
            VALUES {", ".join(values)}
            ON CONFLICT (symbol, timeframe, open_time)
            DO UPDATE SET close_price=EXCLUDED.close_price, volume=EXCLUDED.volume,
                          high_price=EXCLUDED.high_price, low_price=EXCLUDED.low_price
        """
        session = get_session()
        try:
            session.execute(text(sql), params)
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
