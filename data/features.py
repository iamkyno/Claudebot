import pandas as pd
import numpy as np
import ta


def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["rsi"] = ta.momentum.RSIIndicator(df["close"], window=14).rsi()

    macd = ta.trend.MACD(df["close"])
    df["macd"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_diff"] = macd.macd_diff()

    bb = ta.volatility.BollingerBands(df["close"], window=20, window_dev=2)
    df["bb_upper"] = bb.bollinger_hband()
    df["bb_lower"] = bb.bollinger_lband()
    df["bb_mid"] = bb.bollinger_mavg()
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"]
    bb_range = df["bb_upper"] - df["bb_lower"]
    df["bb_position"] = (df["close"] - df["bb_lower"]) / bb_range.replace(0, np.nan)

    df["ema_9"] = ta.trend.EMAIndicator(df["close"], window=9).ema_indicator()
    df["ema_21"] = ta.trend.EMAIndicator(df["close"], window=21).ema_indicator()
    df["ema_50"] = ta.trend.EMAIndicator(df["close"], window=50).ema_indicator()
    df["ema_200"] = ta.trend.EMAIndicator(df["close"], window=200).ema_indicator()

    df["atr"] = ta.volatility.AverageTrueRange(
        df["high"], df["low"], df["close"], window=14
    ).average_true_range()

    df["volume_sma"] = df["volume"].rolling(20).mean()
    df["volume_ratio"] = df["volume"] / df["volume_sma"].replace(0, np.nan)

    df["obv"] = ta.volume.OnBalanceVolumeIndicator(df["close"], df["volume"]).on_balance_volume()

    stoch = ta.momentum.StochasticOscillator(df["high"], df["low"], df["close"])
    df["stoch_k"] = stoch.stoch()
    df["stoch_d"] = stoch.stoch_signal()

    df["price_change_1h"] = df["close"].pct_change(1)
    df["price_change_4h"] = df["close"].pct_change(4)
    df["price_change_24h"] = df["close"].pct_change(24)

    df["adx"] = ta.trend.ADXIndicator(df["high"], df["low"], df["close"]).adx()
    df["roc"] = ta.momentum.ROCIndicator(df["close"], window=12).roc()

    # VWAP resets at midnight UTC each day so scalpers get a true intraday mean.
    typical = (df["high"] + df["low"] + df["close"]) / 3
    dates = df.index.normalize()
    df["vwap"] = (
        (typical * df["volume"]).groupby(dates).cumsum()
        / df["volume"].groupby(dates).cumsum().replace(0, np.nan)
    )

    return df


def get_feature_columns() -> list:
    return [
        "rsi", "macd", "macd_signal", "macd_diff",
        "bb_upper", "bb_lower", "bb_position", "bb_width",
        "ema_9", "ema_21", "ema_50",
        "atr", "volume_ratio", "obv",
        "stoch_k", "stoch_d",
        "price_change_1h", "price_change_4h", "price_change_24h",
        "adx", "roc",
    ]
