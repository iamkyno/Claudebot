import json
import logging
import threading
import time
from datetime import datetime

from sqlalchemy import text

from data.db import get_session

logger = logging.getLogger(__name__)

# Binance futures all-liquidations stream
_STREAM_URL = "wss://fstream.binance.com/ws/!forceOrder@arr"


class LiquidationFeed:
    """
    Subscribes to Binance's real-time force-order WebSocket stream and writes
    every liquidation event to the liquidation_events table so
    LiquidationCascadeStrategy can consume them.
    """

    def __init__(self):
        self._thread: threading.Thread | None = None
        self._running = False
        self._ws = None

    def start(self):
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="liq-feed"
        )
        self._thread.start()
        logger.info("Liquidation feed started")

    def stop(self):
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ #

    def _run(self):
        import websocket  # noqa: PLC0415  (lazy import keeps startup fast)

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
                logger.warning(f"Liquidation WS crashed: {e}")

            if self._running:
                logger.info(f"Liquidation feed reconnecting in {backoff}s…")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _on_message(self, _ws, raw: str):
        try:
            msg = json.loads(raw)
            order = msg.get("o", {})
            raw_symbol = order.get("s", "")       # e.g. "BTCUSDT"
            side_raw = order.get("S", "")          # "SELL"=long liq, "BUY"=short liq
            filled_qty = float(order.get("z", 0))
            avg_price = float(order.get("ap") or order.get("p", 0))
            ts_ms = int(order.get("T", 0))

            if not raw_symbol or avg_price <= 0 or filled_qty <= 0:
                return

            # Normalise "BTCUSDT" → "BTC/USDT"
            if raw_symbol.endswith("USDT"):
                symbol = raw_symbol[:-4] + "/USDT"
            else:
                symbol = raw_symbol

            side = "long" if side_raw == "SELL" else "short"
            usd_value = filled_qty * avg_price
            event_time = datetime.utcfromtimestamp(ts_ms / 1000)

            self._persist(symbol, side, filled_qty, avg_price, usd_value, event_time)
        except Exception as e:
            logger.debug(f"Liquidation parse error: {e}")

    def _persist(self, symbol, side, qty, price, usd_value, event_time):
        session = get_session()
        try:
            session.execute(text("""
                INSERT INTO liquidation_events
                    (symbol, side, quantity, price, usd_value, event_time)
                VALUES
                    (:symbol, :side, :qty, :price, :usd, :ts)
            """), {
                "symbol": symbol, "side": side, "qty": qty,
                "price": price, "usd": usd_value, "ts": event_time,
            })
            session.commit()
            if usd_value >= 1_000_000:
                logger.info(
                    f"Large liquidation: {side.upper()} {symbol} "
                    f"${usd_value:,.0f} @ {price:.2f}"
                )
        except Exception as e:
            session.rollback()
            logger.debug(f"Liquidation persist failed: {e}")
        finally:
            session.close()

    @staticmethod
    def _on_error(_ws, error):
        logger.warning(f"Liquidation WS error: {error}")

    @staticmethod
    def _on_close(_ws, code, msg):
        logger.debug(f"Liquidation WS closed: {code} {msg}")
