"""
Aggressor-flow stream — who is actually hitting the tape.

Binance's aggTrade stream marks every trade with whether the BUYER was the
maker (m=true → an aggressive SELL hit the bid) or the taker (m=false → an
aggressive BUY lifted the ask). A rolling window of that gives taker-flow:

    flow = aggressive_buy_volume / total_volume     (0..1, 0.5 = balanced)

This is the classic microstructure predictor for short-horizon moves — the
order *book* shows resting intent, the *tape* shows real commitment. Fed to
the ML as `taker_flow`. Same daemon-thread + reconnect pattern as the other
feeds; a missing reading is None (neutral) upstream.
"""

import json
import logging
import threading
import time
from collections import deque

logger = logging.getLogger(__name__)

_WS_BASE = "wss://stream.binance.com:9443/stream?streams="


class TradeFlowStream:
    def __init__(self, symbols: list[str], window_seconds: int = 60):
        self.window = window_seconds
        self._symbols = [s.replace("/", "").lower() for s in symbols]
        self._thread: threading.Thread | None = None
        self._running = False
        self._ws = None
        self._lock = threading.Lock()
        # flat symbol -> deque[(ts, qty, is_aggressive_buy)]
        self._tape: dict[str, deque] = {}
        self._last_msg_at = 0.0

    def start(self):
        if not self._symbols:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="trade-flow")
        self._thread.start()
        logger.info(f"Trade-flow stream started ({len(self._symbols)} symbols, "
                    f"{self.window}s window)")

    def stop(self):
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ #

    def flow(self, symbol: str) -> float | None:
        """Aggressive-buy share of volume in the window (0..1), or None."""
        key = symbol.replace("/", "").upper()
        cutoff = time.time() - self.window
        with self._lock:
            tape = self._tape.get(key)
            if not tape:
                return None
            rows = [(q, b) for ts, q, b in tape if ts >= cutoff]
        if not rows:
            return None
        total = sum(q for q, _ in rows)
        if total <= 0:
            return None
        buys = sum(q for q, b in rows if b)
        return buys / total

    def is_live(self, max_staleness: float = 20.0) -> bool:
        return (time.time() - self._last_msg_at) < max_staleness

    # ------------------------------------------------------------------ #

    def _run(self):
        import websocket  # lazy import

        url = _WS_BASE + "/".join(f"{s}@aggTrade" for s in self._symbols)
        backoff = 2
        while self._running:
            try:
                self._ws = websocket.WebSocketApp(
                    url,
                    on_message=self._on_message,
                    on_error=lambda _w, e: logger.debug(f"Trade-flow WS error: {e}"),
                    on_close=lambda _w, c, m: logger.debug(f"Trade-flow WS closed: {c}"),
                )
                self._ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                logger.warning(f"Trade-flow stream crashed: {e}")
            if self._running:
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _on_message(self, _ws, raw: str):
        try:
            d = json.loads(raw).get("data", {})
            sym = d.get("s")
            qty = float(d.get("q", 0))
            if not sym or qty <= 0:
                return
            # m=True -> buyer was maker -> the AGGRESSOR sold. Buy-aggression
            # is therefore m=False.
            aggressive_buy = not d.get("m", False)
            now = time.time()
            with self._lock:
                tape = self._tape.setdefault(sym, deque(maxlen=5000))
                tape.append((now, qty, aggressive_buy))
            self._last_msg_at = now
        except Exception as e:
            logger.debug(f"Trade-flow parse error: {e}")
