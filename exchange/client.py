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

    def get_min_order_amount(self, symbol: str) -> float:
        info = self.get_symbol_info(symbol)
        if info:
            return info.get("limits", {}).get("amount", {}).get("min", 0)
        return 0

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
