import time
import logging
from functools import wraps
import ccxt

from config.settings import get_config, get_secrets

logger = logging.getLogger(__name__)


def retry(max_attempts=3, base_delay=2.0):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except (ccxt.NetworkError, ccxt.RequestTimeout) as e:
                    if attempt == max_attempts - 1:
                        raise
                    delay = base_delay * (2 ** attempt)
                    logger.warning(f"{func.__name__} network error, retry in {delay}s: {e}")
                    time.sleep(delay)
        return wrapper
    return decorator


class BinanceClient:
    def __init__(self):
        secrets = get_secrets()
        self.config = get_config()

        api_key = secrets["binance"]["api_key"]
        api_secret = secrets["binance"]["api_secret"]
        self.has_keys = bool(api_key and api_secret)

        auth_params = {"enableRateLimit": True}
        if self.has_keys:
            auth_params["apiKey"] = api_key
            auth_params["secret"] = api_secret
        else:
            # Public-data-only mode — enough for paper trading and backtests.
            logger.info("No Binance API keys found — running in public-data mode "
                        "(paper trading only).")

        # Authenticated instances (order placement, balance)
        self.spot    = ccxt.binance({**auth_params, "options": {"defaultType": "spot"}})
        self.futures = ccxt.binance({**auth_params, "options": {"defaultType": "future"}})

        # Keyless instance for public market data (OHLCV, tickers, orderbook).
        # When API keys have IP restrictions, Binance rejects the request even
        # on public endpoints if the authenticated key header is included — so
        # we strip the key for all read-only market-data calls.
        pub = {"enableRateLimit": True}
        self.public = ccxt.binance({**pub, "options": {"defaultType": "spot"}})

        # Symbols already configured for futures trading (margin mode + leverage).
        self._futures_ready: set[str] = set()

        if self.config.get("binance", {}).get("testnet"):
            self.spot.set_sandbox_mode(True)
            self.futures.set_sandbox_mode(True)

    @retry()
    def fetch_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 500):
        return self.public.fetch_ohlcv(symbol, timeframe, limit=limit)

    @retry()
    def fetch_ticker(self, symbol: str):
        return self.public.fetch_ticker(symbol)

    @retry()
    def fetch_balance(self):
        if not self.has_keys:
            return {}
        return self.spot.fetch_balance()

    @retry()
    def fetch_orderbook(self, symbol: str, limit: int = 20):
        return self.public.fetch_order_book(symbol, limit=limit)

    @retry()
    def create_market_order(self, symbol: str, side: str, amount: float):
        return self.spot.create_market_order(symbol, side, amount)

    @retry()
    def create_limit_order(self, symbol: str, side: str, amount: float, price: float):
        return self.spot.create_limit_order(symbol, side, amount, price)

    @retry()
    def cancel_order(self, order_id: str, symbol: str):
        return self.spot.cancel_order(order_id, symbol)

    @retry()
    def fetch_order(self, order_id: str, symbol: str):
        return self.spot.fetch_order(order_id, symbol)

    @retry()
    def fetch_open_orders(self, symbol: str = None):
        return self.spot.fetch_open_orders(symbol)

    @retry()
    def fetch_funding_rate(self, symbol: str):
        return self.futures.fetch_funding_rate(symbol)

    def get_symbol_info(self, symbol: str):
        markets = self.public.load_markets()
        return markets.get(symbol)

    def get_min_order_amount(self, symbol: str, venue: str = "spot") -> float:
        if venue == "futures":
            try:
                markets = self.futures.load_markets()
                info = markets.get(self._perp(symbol)) or markets.get(symbol)
                if info:
                    return info.get("limits", {}).get("amount", {}).get("min", 0) or 0
            except Exception as e:
                logger.debug(f"Futures min-qty lookup failed for {symbol}: {e}")
            return 0
        info = self.get_symbol_info(symbol)
        if info:
            return info.get("limits", {}).get("amount", {}).get("min", 0)
        return 0

    # -- futures (shorts) ------------------------------------------------ #

    @staticmethod
    def _perp(symbol: str) -> str:
        """'BTC/USDT' -> 'BTC/USDT:USDT' (ccxt unified USDT-M perp symbol)."""
        return symbol if ":" in symbol else f"{symbol}:USDT"

    def prepare_futures_symbol(self, symbol: str, leverage: int = 2,
                               margin_mode: str = "isolated"):
        """
        One-time-per-symbol setup before a live short: margin mode + leverage.
        Both calls are best-effort — Binance errors if already set, which is fine.
        """
        perp = self._perp(symbol)
        if perp in self._futures_ready:
            return
        try:
            self.futures.set_margin_mode(margin_mode, perp)
        except Exception as e:
            logger.debug(f"set_margin_mode({perp}): {e}")
        try:
            self.futures.set_leverage(leverage, perp)
        except Exception as e:
            logger.debug(f"set_leverage({perp}): {e}")
        self._futures_ready.add(perp)

    @retry()
    def open_futures_position(self, symbol: str, side: str, amount: float):
        """Open a futures position (side='sell' opens a short)."""
        if not self.has_keys:
            raise RuntimeError("Futures order requires API keys")
        self.prepare_futures_symbol(
            symbol,
            leverage=self.config.get("futures", {}).get("leverage", 2),
            margin_mode=self.config.get("futures", {}).get("margin_mode", "isolated"),
        )
        return self.futures.create_market_order(self._perp(symbol), side, amount)

    @retry()
    def place_post_only(self, symbol: str, side: str, amount: float,
                        price: float, venue: str = "futures"):
        """Rest a maker-only limit order at `price`. GTX (futures) and
        LIMIT_MAKER (spot) are rejected by Binance if they would cross the
        book and take — guaranteeing the maker fee when they fill."""
        if not self.has_keys:
            raise RuntimeError("Post-only order requires API keys")
        if venue == "futures":
            self.prepare_futures_symbol(
                symbol,
                leverage=self.config.get("futures", {}).get("leverage", 2),
                margin_mode=self.config.get("futures", {}).get("margin_mode", "isolated"),
            )
            return self.futures.create_limit_order(
                self._perp(symbol), side, amount, price,
                params={"timeInForce": "GTX"},
            )
        return self.spot.create_order(symbol, "LIMIT_MAKER", side, amount, price)

    def wait_fill(self, order_id: str, symbol: str, venue: str = "futures",
                  timeout: float = 10.0):
        """Poll an order until filled or timeout; cancel on timeout.
        Returns the filled order dict, or None if it never filled."""
        ex = self.futures if venue == "futures" else self.spot
        sym = self._perp(symbol) if venue == "futures" else symbol
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                o = ex.fetch_order(order_id, sym)
            except Exception:
                time.sleep(0.5)
                continue
            status = o.get("status")
            if status == "closed":
                return o
            if status in ("canceled", "rejected", "expired"):
                return None
            time.sleep(0.5)
        try:
            ex.cancel_order(order_id, sym)
        except Exception:
            pass
        return None

    def place_protective_stop(self, symbol: str, position_side: str,
                              amount: float, stop_price: float,
                              venue: str = "spot"):
        """
        Park a STOP-MARKET order ON THE EXCHANGE so the position stays
        protected even when the bot is down. position_side is the side of the
        open position: a long is protected by a stop-SELL, a short by a
        stop-BUY. Returns the order dict, or None (caller logs 'unprotected').
        """
        close_side = "sell" if position_side == "buy" else "buy"
        try:
            if venue == "futures" or position_side == "sell":
                return self.futures.create_order(
                    self._perp(symbol), "STOP_MARKET", close_side, amount, None,
                    {"stopPrice": stop_price, "reduceOnly": True},
                )
            # Spot STOP_LOSS = market order triggered at stopPrice.
            return self.spot.create_order(
                symbol, "STOP_LOSS", close_side, amount, None,
                {"stopPrice": stop_price},
            )
        except Exception as e:
            logger.warning(f"Protective stop failed for {symbol} @ {stop_price}: {e}")
            return None

    def cancel_protective_stop(self, symbol: str, order_id: str, venue: str = "spot"):
        """Best-effort cancel of a parked stop (it may have already fired)."""
        try:
            if venue == "futures":
                self.futures.cancel_order(order_id, self._perp(symbol))
            else:
                self.spot.cancel_order(order_id, symbol)
        except Exception as e:
            logger.debug(f"Protective cancel {order_id} ({symbol}): {e}")

    @retry()
    def close_futures_position(self, symbol: str, amount: float,
                               close_side: str = "buy"):
        """Close a futures position with reduceOnly so the order can only
        shrink the position, never flip it (buy closes shorts, sell closes
        futures longs)."""
        return self.futures.create_market_order(
            self._perp(symbol), close_side, amount, params={"reduceOnly": True}
        )

    def get_orderbook_imbalance(self, symbol: str) -> float:
        """Returns ratio of bid volume to total volume at top 10 levels."""
        try:
            book = self.fetch_orderbook(symbol, limit=10)
            bid_vol = sum(b[1] for b in book["bids"])
            ask_vol = sum(a[1] for a in book["asks"])
            total = bid_vol + ask_vol
            return bid_vol / total if total > 0 else 0.5
        except Exception:
            return 0.5
