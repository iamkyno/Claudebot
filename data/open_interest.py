"""
Open-interest change tracker — a crowding signal the candles can't see.

OI rising while price falls = shorts piling in; OI falling on a rally =
short-covering, not fresh demand. We track the percent change in OI per
symbol over a ~1h window and feed it to the ML as `oi_change`.

Keyless (public futures endpoint via ccxt), snapshot kept in memory,
None-safe everywhere: a missing reading simply becomes a neutral feature.
"""

import logging
import threading
import time

logger = logging.getLogger(__name__)


class OpenInterestTracker:
    def __init__(self, exchange_client, window_seconds: int = 3600,
                 fetch_ttl: int = 300):
        self.client = exchange_client
        self.window = window_seconds
        self.ttl = fetch_ttl
        self._lock = threading.Lock()
        # symbol -> list[(ts, oi)] — small ring of recent snapshots
        self._hist: dict[str, list] = {}
        self._last_fetch: dict[str, float] = {}

    def _fetch(self, symbol: str) -> float | None:
        try:
            perp = symbol if ":" in symbol else f"{symbol}:USDT"
            data = self.client.futures.fetch_open_interest(perp)
            oi = data.get("openInterestAmount") or data.get("openInterestValue")
            return float(oi) if oi else None
        except Exception as e:
            logger.debug(f"OI fetch failed for {symbol}: {e}")
            return None

    def change(self, symbol: str) -> float | None:
        """Fractional OI change over ~the window (e.g. +0.05 = +5%), or None."""
        now = time.time()
        with self._lock:
            fresh_enough = (now - self._last_fetch.get(symbol, 0)) < self.ttl
            hist = self._hist.setdefault(symbol, [])

        if not fresh_enough:
            oi = self._fetch(symbol)
            with self._lock:
                self._last_fetch[symbol] = now
                if oi:
                    hist.append((now, oi))
                    # keep ~2 windows of snapshots
                    cutoff = now - self.window * 2
                    self._hist[symbol] = [(t, v) for t, v in hist if t >= cutoff]

        with self._lock:
            hist = self._hist.get(symbol, [])
        if len(hist) < 2:
            return None
        newest_t, newest_v = hist[-1]
        # Oldest snapshot inside the window is the baseline.
        base = next(((t, v) for t, v in hist if t >= newest_t - self.window), hist[0])
        if not base[1]:
            return None
        return (newest_v - base[1]) / base[1]
