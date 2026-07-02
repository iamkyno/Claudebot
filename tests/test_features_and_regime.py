"""Feature pipeline + regime classifier tests."""

import numpy as np
import pandas as pd
import pytest

from data.features import compute_features, get_feature_columns
from data.regime import classify_regime, regime_allows, TRENDING, RANGING


def _df(hours=120, start="2024-01-01", trend=0.0, seed=7):
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=hours, freq="h")
    base = 100 + trend * np.arange(hours) + rng.normal(0, 0.3, hours).cumsum()
    return pd.DataFrame({
        "open": base, "high": base * 1.004, "low": base * 0.996,
        "close": base, "volume": rng.uniform(8, 20, hours),
    }, index=idx)


class TestFeatures:
    def test_ml_columns_exist_with_h_suffix(self):
        df = compute_features(_df())
        for col in ["price_change_1h", "price_change_4h", "price_change_24h",
                    "rsi", "atr", "vwap", "bb_position", "volume_ratio"]:
            assert col in df.columns, col
        assert set(get_feature_columns()).issubset(df.columns)

    def test_vwap_resets_each_utc_day(self):
        df = compute_features(_df(hours=48))
        day2_first = df.iloc[24]     # first bar of the second UTC day
        typical = (day2_first["high"] + day2_first["low"] + day2_first["close"]) / 3
        # At the first bar of a fresh session, VWAP == that bar's typical price.
        assert day2_first["vwap"] == pytest.approx(typical)

    def test_range_index_fallback_does_not_crash(self):
        df = _df().reset_index(drop=True)
        out = compute_features(df)
        assert "vwap" in out.columns and out["vwap"].notna().any()


class TestRegime:
    def test_strong_trend_classified_trending(self):
        df = compute_features(_df(trend=0.5))
        r = classify_regime(df)
        assert r["trend"] == TRENDING
        assert r["direction"] == "up"

    def test_flat_noise_classified_ranging(self):
        df = compute_features(_df(trend=0.0))
        r = classify_regime(df)
        assert r["trend"] == RANGING

    def test_gates_route_strategies_correctly(self):
        trend = {"trend": TRENDING}
        chop = {"trend": RANGING}
        assert regime_allows("ema_cross", trend) and not regime_allows("ema_cross", chop)
        assert regime_allows("rsi_bb", chop) and not regime_allows("rsi_bb", trend)
        # unlisted strategies run anywhere
        assert regime_allows("scalp", trend) and regime_allows("scalp", chop)

    def test_short_frame_defaults_safe(self):
        r = classify_regime(compute_features(_df(hours=20)))
        assert r["trend"] in (TRENDING, RANGING)
