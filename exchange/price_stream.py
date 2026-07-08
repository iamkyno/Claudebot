"""
Real-time price stream.

A single Binance WebSocket (`!ticker@arr`) pushes the last price of every
trading pair roughly once a second. We keep an in-memory `{symbol: price}`
map so the rest of the bot can read a millisecond-fresh price instead of
waiting for the 60-second REST polling loop. This is what makes the scalper
actually able to scalp and trailing stops able to trail.

Background daemon thread, auto-reconnect with backoff — same pattern as
LiquidationFeed. If the socket is down, get_price() returns None and callers
fall back to REST.
"""

import json
import logging
import threading
import time

logger = logging.getLogger(__name__)

_STREAM_URL = "wss://stream.binance.com:9443/ws/!ticker@arr"


class PriceStream:
    def __init__(self):
        self._thread: threading.Thread | None = None
        self._running = False
        self._ws = None
        self._lock = threading.Lock()
        self._prices: dict[str, float] = {}
        self._updated_at: dict[str, float] = {}
        self._last_msg_at = 0.0

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="price-stream")
        self._thread.start()
        logger.info("Price stream started")

    def stop(self):
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ #

    def get_price(self, symbol: str) -> float | None:
        """Latest streamed price for a 'BTC/USDT'-style symbol, or None."""
        key = symbol.replace("/", "")
        with self._lock:
            return self._prices.get(key)

    def is_live(self, max_staleness: float = 10.0) -> bool:
        """True if we've received a message recently."""
        return (time.time() - self._last_msg_at) < max_staleness

    def kick(self):
        """Force-close a zombie connection so the run-loop reconnects NOW
        instead of waiting out its backoff. Safe to call anytime."""
        logger.warning("Price stream kicked — forcing reconnect")
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ #

    def _run(self):
        import websocket  # lazy import

        backoff = 2
        while self._running:
            try:
                self._ws = websocket.WebSocketApp(
                    _STREAM_URL,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                logger.warning(f"Price stream crashed: {e}")
            if self._running:
                logger.info(f"Price stream reconnecting in {backoff}s…")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _on_message(self, _ws, raw: str):
        try:
            arr = json.loads(raw)
            now = time.time()
            updates = {}
            for t in arr:
                sym = t.get("s")        # "BTCUSDT"
                last = t.get("c")       # last price
                if sym and last:
                    updates[sym] = float(last)
            with self._lock:
                self._prices.update(updates)
                for s in updates:
                    self._updated_at[s] = now
            # First message after a stale spell = recovery worth announcing.
            if self._last_msg_at and (now - self._last_msg_at) > 30:
                logger.info(f"Price stream RECOVERED after "
                            f"{now - self._last_msg_at:.0f}s gap — back to "
                            f"real-time prices")
            self._last_msg_at = now
        except Exception as e:
            logger.debug(f"Price stream parse error: {e}")

    @staticmethod
    def _on_error(_ws, error):
        # WARNING, not debug: when the stream is failing, the WHY must be in
        # the user's log, not hidden behind a debug flag.
        logger.warning(f"Price stream error: {error}")

    @staticmethod
    def _on_close(_ws, code, msg):
        logger.info(f"Price stream closed: {code} {msg}")
