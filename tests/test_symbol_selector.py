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


class TestVolumeRanking:
    def test_scalp_list_ranks_by_pure_volume_not_score(self):
        # LOWCAP pumps 40% on $60M volume -> tops the SCORE list; the scalp
        # list must still prefer the deeper BTC/ETH books.
        tickers = {
            "LOWCAP/USDT": _ticker(0.027, 0.40, quote_vol=60_000_000),
            "BTC/USDT": _ticker(65000, 0.02, quote_vol=900_000_000),
            "ETH/USDT": _ticker(3200, 0.02, quote_vol=500_000_000),
        }
        sel = SymbolSelector(FakeExchange(tickers), max_symbols=10, min_volume_usdt=1_000_000)
        sel.get_symbols(refresh=True)
        assert sel.top_by_volume(2) == ["BTC/USDT", "ETH/USDT"]

    def test_min_volume_floor_excludes_thin_books(self):
        tickers = {
            "THIN/USDT": _ticker(1.5, 0.10, quote_vol=20_000_000),
            "BTC/USDT": _ticker(65000, 0.02, quote_vol=900_000_000),
        }
        sel = SymbolSelector(FakeExchange(tickers), max_symbols=10, min_volume_usdt=1_000_000)
        sel.get_symbols(refresh=True)
        assert sel.top_by_volume(5, min_volume=50_000_000) == ["BTC/USDT"]

    def test_fallback_before_first_refresh(self):
        sel = SymbolSelector(FakeExchange({}), max_symbols=10)
        assert len(sel.top_by_volume(3)) == 3   # falls back to defaults


class TestStrategyToggle:
    def test_enabled_flag_respected_by_base(self):
        from strategies.grid import GridStrategy
        assert GridStrategy({"enabled": False}).is_enabled() is False
        assert GridStrategy({}).is_enabled() is True
