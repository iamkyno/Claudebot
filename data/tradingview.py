"""
TradingView technical-analysis consensus as an extra, keyless signal source.

We use the `tradingview-ta` Python library (not the TradingView MCP server):
an MCP server is meant to be driven interactively by an AI client, whereas this
bot runs an autonomous 24/7 loop and just needs the data inline. The library
returns TradingView's own aggregated oscillator + moving-average verdict
(STRONG_BUY … STRONG_SELL) with no API key required.

The result is normalised to a score in [-1, 1] that feeds both a soft buy veto
and the ML feature vector (column `tv_recommendation`).
"""

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from tradingview_ta import TA_Handler, Interval
    _TV_OK = True
except ImportError:
    _TV_OK = False

_INTERVAL_MAP = {
    "1m": "INTERVAL_1_MINUTE", "5m": "INTERVAL_5_MINUTES",
    "15m": "INTERVAL_15_MINUTES", "30m": "INTERVAL_30_MINUTES",
    "1h": "INTERVAL_1_HOUR", "2h": "INTERVAL_2_HOURS",
    "4h": "INTERVAL_4_HOURS", "1d": "INTERVAL_1_DAY",
}


class TradingViewAnalyzer:
    """Cached, fail-safe wrapper around TradingView's TA consensus."""

    def __init__(self, timeframe: str = "1h", screener: str = "crypto",
                 exchange: str = "BINANCE", cache_ttl: int = 300):
        self.enabled = _TV_OK
        self.screener = screener
        self.exchange = exchange
        self.cache_ttl = cache_ttl
        interval_name = _INTERVAL_MAP.get(timeframe, "INTERVAL_1_HOUR")
        self._interval = getattr(Interval, interval_name) if _TV_OK else None
        self._cache: dict[str, tuple[float, float]] = {}  # symbol -> (ts, score)

        if not _TV_OK:
            logger.info("tradingview-ta not installed — TradingView consensus disabled.")

    def score(self, symbol: str) -> Optional[float]:
        """
        Return a consensus score in [-1, 1] (positive = bullish), or None if
        unavailable. Cached per symbol for `cache_ttl` seconds.
        """
        if not self.enabled:
            return None

        now = time.time()
        cached = self._cache.get(symbol)
        if cached and now - cached[0] < self.cache_ttl:
            return cached[1]

        tv_symbol = symbol.replace("/", "")  # BTC/USDT -> BTCUSDT
        try:
            handler = TA_Handler(
                symbol=tv_symbol, screener=self.screener,
                exchange=self.exchange, interval=self._interval,
            )
            summary = handler.get_analysis().summary
            buy = summary.get("BUY", 0)
            sell = summary.get("SELL", 0)
            neutral = summary.get("NEUTRAL", 0)
            total = buy + sell + neutral
            score = (buy - sell) / total if total else 0.0
            self._cache[symbol] = (now, score)
            return score
        except Exception as e:
            logger.debug(f"TradingView lookup failed for {symbol}: {e}")
            # Cache a neutral result briefly so we don't hammer a failing endpoint.
            self._cache[symbol] = (now, 0.0)
            return 0.0
