"""
Free market-wide sentiment / context layer.

Three keyless signals the per-symbol indicators can't see:
  1. Fear & Greed index   (alternative.me) — fade crowd extremes.
  2. BTC dominance         (CoinGecko)      — rising = alt risk-off.
  3. Aggregate funding     (Binance futures) — whole-market leverage skew.

Everything is cached with a TTL so we hit the network at most a few times an
hour. All calls are wrapped so a dead endpoint never breaks a trading tick —
a missing signal just returns None and is treated as neutral upstream.
"""

import logging
import threading
import time

import requests

logger = logging.getLogger(__name__)

_FNG_URL = "https://api.alternative.me/fng/?limit=1"
_GLOBAL_URL = "https://api.coingecko.com/api/v3/global"
_FUNDING_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"


class SentimentFeed:
    def __init__(self, ttl_seconds: int = 900):
        self.ttl = ttl_seconds
        self._lock = threading.Lock()
        self._cache: dict = {}
        self._fetched_at: dict = {}

    # ------------------------------------------------------------------ #

    def _cached(self, key: str, fn):
        now = time.time()
        with self._lock:
            if key in self._cache and (now - self._fetched_at.get(key, 0)) < self.ttl:
                return self._cache[key]
        try:
            val = fn()
        except Exception as e:
            logger.debug(f"Sentiment '{key}' fetch failed: {e}")
            val = self._cache.get(key)  # serve stale rather than nothing
        with self._lock:
            self._cache[key] = val
            self._fetched_at[key] = now
        return val

    # ------------------------------------------------------------------ #

    def fear_greed(self) -> int | None:
        """0–100. <25 = extreme fear (contrarian buy), >75 = extreme greed."""
        def _fetch():
            r = requests.get(_FNG_URL, timeout=8)
            data = r.json()["data"][0]
            return int(data["value"])
        return self._cached("fng", _fetch)

    def btc_dominance(self) -> float | None:
        """BTC market-cap dominance %, e.g. 52.3."""
        def _fetch():
            r = requests.get(_GLOBAL_URL, timeout=8)
            return float(r.json()["data"]["market_cap_percentage"]["btc"])
        return self._cached("btc_dom", _fetch)

    def aggregate_funding(self) -> float | None:
        """
        Mean funding rate across all USDT perps. Strongly positive = market is
        crowded long (over-leveraged) → fade risk; strongly negative = crowded
        short. Returned as a fraction per 8h (e.g. 0.0001 = 0.01%).
        """
        def _fetch():
            r = requests.get(_FUNDING_URL, timeout=8)
            rows = r.json()
            rates = [
                float(x["lastFundingRate"]) for x in rows
                if str(x.get("symbol", "")).endswith("USDT") and x.get("lastFundingRate")
            ]
            return sum(rates) / len(rates) if rates else None
        return self._cached("agg_funding", _fetch)

    # ------------------------------------------------------------------ #

    def snapshot(self) -> dict:
        """All three signals plus derived bias flags for the orchestrator."""
        fng = self.fear_greed()
        dom = self.btc_dominance()
        funding = self.aggregate_funding()

        # Derived gates. None-safe: unknown signal → neutral.
        extreme_greed = fng is not None and fng >= 80      # crowd euphoric → caution
        extreme_fear = fng is not None and fng <= 20       # capitulation → contrarian long ok
        overleveraged_long = funding is not None and funding >= 0.0005  # >0.05%/8h

        return {
            "fear_greed": fng,
            "btc_dominance": dom,
            "aggregate_funding": funding,
            "extreme_greed": extreme_greed,
            "extreme_fear": extreme_fear,
            "overleveraged_long": overleveraged_long,
        }
