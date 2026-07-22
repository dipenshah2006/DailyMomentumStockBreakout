"""
data_fetch.py
Downloads full historical daily OHLCV data from yfinance for NSE symbols,
with an incremental parquet cache (only fetches bars newer than what is
already cached) - mirrors the caching convention already used in
DailyMomentumStockBreakout / multibagger_report.py.
"""
from __future__ import annotations
import threading
import time
from pathlib import Path

import pandas as pd
import yfinance as yf

_PARQUET_LOCK = threading.Lock()
_YF_DOWNLOAD_LOCK = threading.Lock()  # yfinance has known thread-safety issues
                                       # ("dictionary changed size during iteration")
                                       # when yf.download() is called concurrently
                                       # from multiple threads - serialize the actual
                                       # network call while keeping everything else
                                       # (indicator calc, chart gen) parallel.
_MAX_RETRIES = 3
_RETRY_BACKOFF_SEC = 3

_TZ_CACHE_DIR = Path(__file__).resolve().parent / ".yf_tz_cache"
try:
    _TZ_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    yf.set_tz_cache_location(str(_TZ_CACHE_DIR))
except Exception:
    pass


def _extract_ticker_df(raw: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """Handles both old (ticker on level 0) and new (metric on level 0)
    yfinance MultiIndex column layouts, and de-duplicates columns."""
    if isinstance(raw.columns, pd.MultiIndex):
        lvl0 = set(raw.columns.get_level_values(0))
        if ticker in lvl0:
            df = raw[ticker].copy()
        else:
            df = raw.xs(ticker, axis=1, level=1, drop_level=True) if ticker in raw.columns.get_level_values(1) else raw.droplevel(0, axis=1)
    else:
        df = raw.copy()
    df = df.loc[:, ~df.columns.duplicated()]
    keep = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
    return df[keep].dropna(how="all")


def _download_with_retry(ticker: str, **kwargs) -> pd.DataFrame:
    """Serializes yf.download() calls across threads (works around yfinance's
    internal thread-safety bugs) and retries transient network errors."""
    last_err = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            with _YF_DOWNLOAD_LOCK:
                return yf.download(ticker, progress=False, auto_adjust=True,
                                    threads=False, **kwargs)
        except Exception as e:
            last_err = e
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_BACKOFF_SEC * attempt)
    print(f"[FETCH-ERROR] {ticker}: {last_err} (after {_MAX_RETRIES} attempts)")
    return pd.DataFrame()


def get_history(symbol: str, cache_dir: Path, suffix: str = ".NS",
                 period: str = "max") -> pd.DataFrame | None:
    """
    Returns full daily OHLCV history for `symbol`, using / updating a
    per-symbol parquet cache under cache_dir.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"{symbol}.parquet"
    ticker = f"{symbol}{suffix}"

    cached = None
    if cache_file.exists():
        with _PARQUET_LOCK:
            try:
                cached = pd.read_parquet(cache_file)
            except Exception:
                cached = None

    if cached is not None and len(cached) > 0:
        next_start = (cached.index[-1] + pd.Timedelta(days=1)).normalize()
        today = pd.Timestamp.today().normalize()
        if next_start > today:
            new_raw = pd.DataFrame()  # cache already fully up to date
        else:
            new_raw = _download_with_retry(ticker, start=next_start.strftime("%Y-%m-%d"))
    else:
        new_raw = _download_with_retry(ticker, period=period)

    try:
        new_df = _extract_ticker_df(new_raw, ticker) if len(new_raw) else pd.DataFrame()
    except Exception as e:
        print(f"[EXTRACT-ERROR] {ticker}: {e} | columns={list(new_raw.columns)}")
        new_df = pd.DataFrame()

    if new_raw is not None and len(new_raw) == 0 and cached is None:
        print(f"[EMPTY] {ticker}: yfinance returned 0 rows (delisted symbol, wrong suffix, or network/rate-limit issue)")

    if cached is not None and len(new_df):
        full = pd.concat([cached, new_df])
        full = full[~full.index.duplicated(keep="last")].sort_index()
    elif cached is not None:
        full = cached
    else:
        full = new_df

    if full is None or full.empty:
        return None

    if len(new_df) or cached is None:
        with _PARQUET_LOCK:
            try:
                full.to_parquet(cache_file)
            except Exception:
                pass

    return full
