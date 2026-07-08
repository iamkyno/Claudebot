"""Symbol selector — stablecoin filtering (name-based and structural)."""

import pytest

from data.symbol_selector import SymbolSelector


class FakeSpot:
    def __init__(self, tickers):
        self._tickers = tickers

    def fetch_tickers(self):
        return self._tickers


class FakeExchange:
    def __init__(self, tickers):
        self.spot = FakeSpot(tickers)


def _ticker(last, pct_range, quote_vol=50_000_000):
    """pct_range: 24h (high-low)/last, e.g. 0.02 for a real 2% mover."""
    half = last * pct_range / 2
    return {"last": last, "high": last + half, "low": last - half,
            "quoteVolume": quote_vol, "percentage": pct_range * 100}


class TestStablecoinFiltering:
    def test_named_stablecoin_skipped(self):
        tickers = {"USDC/USDT": _ticker(1.0001, 0.0002),
                   "BTC/USDT": _ticker(65000, 0.03)}
        sel = SymbolSelector(FakeExchange(tickers), max_symbols=10, min_volume_usdt=1_000_000)
        syms = sel.get_symbols(refresh=True)
        assert "USDC/USDT" not in syms and "BTC/USDT" in syms

    def test_unnamed_new_stablecoin_caught_structurally(self):
        # A stablecoin ccxt/Binance just listed, not in any hardcoded list —
        # this is the actual bug: USD1/RLUSD traded as real pairs.
        tickers = {"USD1/USDT": _ticker(0.9996, 0.0003),
                   "ETH/USDT": _ticker(3200, 0.025)}
        sel = SymbolSelector(FakeExchange(tickers), max_symbols=10, min_volume_usdt=1_000_000)
        syms = sel.get_symbols(refresh=True)
        assert "USD1/USDT" not in syms and "ETH/USDT" in syms

    def test_quiet_real_asset_not_falsely_filtered(self):
        # A real asset having a genuinely quiet day (0.6% range) must survive.
        tickers = {"XRP/USDT": _ticker(2.10, 0.006)}
        sel = SymbolSelector(FakeExchange(tickers), max_symbols=10, min_volume_usdt=1_000_000)
        syms = sel.get_symbols(refresh=True)
        assert "XRP/USDT" in syms

    def test_low_volume_still_filtered_independently(self):
        tickers = {"BTC/USDT": _ticker(65000, 0.03, quote_vol=100_000)}
        sel = SymbolSelector(FakeExchange(tickers), max_symbols=10, min_volume_usdt=1_000_000)
        assert sel.get_symbols(refresh=True) == []
