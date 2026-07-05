"""
Historical kline downloader — Binance's free public archive (data.binance.vision).

Monthly 1m zips, no API key, no rate-limit drama. Cached locally under
backtest/history/ so repeat runs are instant.

    from backtest.data import load_history
    df = load_history("BTC/USDT", months=3, market="futures")   # ~130k 1m bars
"""

import io
import logging
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd
import requests

logger = logging.getLogger(__name__)

_CACHE_DIR = Path(__file__).parent / "history"

_COLS = ["open_time", "open", "high", "low", "close", "volume",
         "close_time", "quote_vol", "trades", "taker_base", "taker_quote", "ignore"]


def _month_list(months: int) -> list[str]:
    """The last `months` complete months plus the current partial month
    (served by the per-day fallback since its monthly zip doesn't exist)."""
    today = date.today()
    y, m = today.year, today.month
    out = []
    for _ in range(months):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        out.append(f"{y:04d}-{m:02d}")
    months_list = list(reversed(out))
    months_list.append(f"{today.year:04d}-{today.month:02d}")
    return months_list


def _url(symbol_flat: str, timeframe: str, ym: str, market: str) -> str:
    base = "https://data.binance.vision/data"
    path = "futures/um" if market == "futures" else "spot"
    return (f"{base}/{path}/monthly/klines/{symbol_flat}/{timeframe}/"
            f"{symbol_flat}-{timeframe}-{ym}.zip")


def _to_datetime(series: pd.Series) -> pd.Series:
    """Archive timestamps switched from ms to us in 2025 — sniff the unit."""
    v = float(series.iloc[0])
    unit = "us" if v > 1e14 else ("ms" if v > 1e11 else "s")
    return pd.to_datetime(series.astype("int64"), unit=unit)


def _parse_zip(content: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        with z.open(z.namelist()[0]) as f:
            df = pd.read_csv(f, header=None, names=_COLS)
    # Newer files ship a header row — drop it if present.
    if isinstance(df.iloc[0]["open_time"], str) and not str(df.iloc[0]["open_time"]).isdigit():
        df = df.iloc[1:].reset_index(drop=True)
    df["timestamp"] = _to_datetime(df["open_time"])
    out = df[["timestamp", "open", "high", "low", "close", "volume"]].copy()
    for c in ("open", "high", "low", "close", "volume"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    return out.dropna().set_index("timestamp").sort_index()


def _fetch_month_daily(symbol_flat: str, timeframe: str, ym: str, market: str) -> pd.DataFrame | None:
    """Fallback for a month whose monthly archive isn't published yet (it lags
    a few days after month end): stitch the per-day zips instead."""
    base = "https://data.binance.vision/data"
    path = "futures/um" if market == "futures" else "spot"
    y, m = int(ym[:4]), int(ym[5:7])
    frames = []
    for day in range(1, 32):
        try:
            d = date(y, m, day)
        except ValueError:
            break
        if d >= date.today():
            break
        url = (f"{base}/{path}/daily/klines/{symbol_flat}/{timeframe}/"
               f"{symbol_flat}-{timeframe}-{d.isoformat()}.zip")
        try:
            r = requests.get(url, timeout=60)
            if r.status_code == 200:
                frames.append(_parse_zip(r.content))
        except Exception as e:
            logger.debug(f"daily {d}: {e}")
    if not frames:
        return None
    logger.info(f"{ym}: monthly archive not published — stitched {len(frames)} daily files")
    return pd.concat(frames).sort_index()


def _fetch_month(symbol_flat: str, timeframe: str, ym: str, market: str) -> pd.DataFrame | None:
    _CACHE_DIR.mkdir(exist_ok=True)
    cache = _CACHE_DIR / f"{symbol_flat}-{timeframe}-{ym}-{market}.pkl"
    if cache.exists():
        return pd.read_pickle(cache)

    url = _url(symbol_flat, timeframe, ym, market)
    logger.info(f"Downloading {url}")
    r = requests.get(url, timeout=120)
    if r.status_code != 200:
        out = _fetch_month_daily(symbol_flat, timeframe, ym, market)
        if out is None:
            logger.warning(f"{ym}: HTTP {r.status_code} and no daily files — skipped")
            return None
        # Don't cache a partial month under the monthly key — it would freeze
        # incomplete data. Re-stitched cheaply on the next run instead.
        return out

    out = _parse_zip(r.content)
    out.to_pickle(cache)
    return out


def load_history(symbol: str, months: int = 3, timeframe: str = "1m",
                 market: str = "futures") -> pd.DataFrame:
    """Concatenated OHLCV frame for the last `months` complete months."""
    flat = symbol.replace("/", "")
    frames = []
    for ym in _month_list(months):
        df = _fetch_month(flat, timeframe, ym, market)
        if df is not None and not df.empty:
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    full = pd.concat(frames)
    full = full[~full.index.duplicated(keep="last")].sort_index()
    logger.info(f"{symbol} {timeframe} {market}: {len(full):,} bars "
                f"({full.index[0]} → {full.index[-1]})")
    return full
