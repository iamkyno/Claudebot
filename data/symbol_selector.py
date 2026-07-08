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

    # Known stablecoins/wrapped tokens — kept as a fast-path, but the real
    # filter below is structural (near-zero 24h range vs USDT) so newly
    # listed stablecoins (e.g. USD1, RLUSD) are caught without needing a
    # maintained name list.
    _SKIP = {"USDC", "BUSD", "TUSD", "USDP", "DAI", "FDUSD",
             "WBTC", "BETH", "LDOT", "STETH", "USD1", "RLUSD",
             "USDE", "PYUSD", "USDD", "GUSD", "EURC"}
    # A pair whose 24h high/low range is this tight (as a fraction of price)
    # has nothing for a strategy to trade — a peg, not volatility. 0.5%
    # comfortably passes real assets on a quiet day but catches every
    # USD-pegged stablecoin pair regardless of name.
    _MIN_RANGE_PCT = 0.005

    def __init__(self, exchange_client, max_symbols: int = 20,
                 min_volume_usdt: float = 15_000_000):
        self.exchange = exchange_client
        self.max_symbols = max_symbols
        self.min_volume = min_volume_usdt
        self._cache: List[str] = []
        self._volumes: dict = {}          # symbol -> 24h quote volume
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

                # Structural stablecoin filter: near-zero 24h range means
                # nothing for RSI/BB/EMA/grid to trade — just fee-bleeding
                # noise around a peg.
                last = ticker.get("last") or 0.0
                hi, lo = ticker.get("high") or 0.0, ticker.get("low") or 0.0
                if last > 0 and (hi - lo) / last < self._MIN_RANGE_PCT:
                    continue

                change_abs = abs(ticker.get("percentage") or 0.0)
                # Opportunity score: volume × volatility premium
                score = vol_24h * (1.0 + change_abs / 100.0)
                candidates.append((symbol, score, vol_24h))

            candidates.sort(key=lambda x: x[1], reverse=True)
            self._cache = [sym for sym, _, _ in candidates[: self.max_symbols]]
            self._volumes = {sym: vol for sym, _, vol in candidates}
            self._cached_at = datetime.utcnow()

            logger.info(
                f"Symbol selector refreshed: {len(self._cache)} symbols | "
                f"Top 5: {', '.join(self._cache[:5])}"
            )
            return self._cache

        except Exception as e:
            logger.error(f"Symbol discovery failed: {e}")
            return self._cache or self._FALLBACK

    def top_by_volume(self, n: int, min_volume: float = 0.0) -> List[str]:
        """
        PURE liquidity ranking — for the scalper. The opportunity score above
        deliberately boosts volatile movers, which surfaces pumping microcaps
        whose thin books let price gap through scalp stops (observed: a stop
        planned at -0.18% filling at -0.50%). Scalps want the deepest books,
        not the fastest movers.
        """
        ranked = sorted(
            ((s, v) for s, v in self._volumes.items() if v >= min_volume),
            key=lambda x: x[1], reverse=True,
        )
        out = [s for s, _ in ranked[:n]]
        return out or (self._cache[:n] if self._cache else self._FALLBACK[:n])
