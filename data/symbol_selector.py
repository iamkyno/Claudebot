import logging
from datetime import datetime, timedelta
from typing import List

logger = logging.getLogger(__name__)


class SymbolSelector:
    """
    Auto-discovers the top USDT spot pairs on Binance by 24h volume,
    weighted by recent price volatility for an opportunity score.
    Refreshes on demand; falls back to a safe default list on error.
    """

    _FALLBACK = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT",
                 "XRP/USDT", "ADA/USDT", "DOGE/USDT", "AVAX/USDT"]

    # Stablecoins and wrapped tokens that shouldn't be traded
    _SKIP = {"USDC", "BUSD", "TUSD", "USDP", "DAI", "FDUSD",
             "WBTC", "BETH", "LDOT", "STETH"}

    def __init__(self, exchange_client, max_symbols: int = 20,
                 min_volume_usdt: float = 15_000_000):
        self.exchange = exchange_client
        self.max_symbols = max_symbols
        self.min_volume = min_volume_usdt
        self._cache: List[str] = []
        self._cached_at: datetime | None = None

    # ------------------------------------------------------------------ #

    def get_symbols(self, refresh: bool = False) -> List[str]:
        if not refresh and self._cache:
            return self._cache
        try:
            tickers = self.exchange.spot.fetch_tickers()
            candidates = []

            for symbol, ticker in tickers.items():
                if not symbol.endswith("/USDT"):
                    continue
                base = symbol.split("/")[0]
                if base in self._SKIP:
                    continue

                vol_24h = ticker.get("quoteVolume") or 0.0
                if vol_24h < self.min_volume:
                    continue

                change_abs = abs(ticker.get("percentage") or 0.0)
                # Opportunity score: volume × volatility premium
                score = vol_24h * (1.0 + change_abs / 100.0)
                candidates.append((symbol, score))

            candidates.sort(key=lambda x: x[1], reverse=True)
            self._cache = [sym for sym, _ in candidates[: self.max_symbols]]
            self._cached_at = datetime.utcnow()

            logger.info(
                f"Symbol selector refreshed: {len(self._cache)} symbols | "
                f"Top 5: {', '.join(self._cache[:5])}"
            )
            return self._cache

        except Exception as e:
            logger.error(f"Symbol discovery failed: {e}")
            return self._cache or self._FALLBACK
