"""
indicators.py
Core technical-indicator + signal-detection library used by
rsi_cci_macd_screener.py

All functions operate on a pandas DataFrame with columns:
    Open, High, Low, Close, Volume
and a DatetimeIndex.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from scipy.signal import argrelextrema


# ----------------------------------------------------------------------
# Basic building blocks
# ----------------------------------------------------------------------
def rma(series: pd.Series, length: int) -> pd.Series:
    """Wilder's RMA (used internally by RSI/CCI-style smoothing)."""
    return series.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()


def rsi(close: pd.Series, length: int = 14) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    roll_up = rma(up, length)
    roll_down = rma(down, length)
    rs = roll_up / roll_down.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    out[roll_down == 0] = 100.0
    return out


def sma(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(length, min_periods=length).mean()


def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False, min_periods=length).mean()


def cci(df: pd.DataFrame, length: int = 200) -> pd.Series:
    tp = (df["High"] + df["Low"] + df["Close"]) / 3.0
    tp_sma = tp.rolling(length, min_periods=length).mean()
    # vectorised mean-absolute-deviation (matches user's existing convention)
    mad = tp.rolling(length, min_periods=length).apply(
        lambda x: np.abs(x - x.mean()).mean(), raw=True
    )
    return (tp - tp_sma) / (0.015 * mad.replace(0, np.nan))


def macd(close: pd.Series, fast: int = 34, slow: int = 200, signal: int = 9):
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def heikin_ashi(df: pd.DataFrame) -> pd.DataFrame:
    ha = pd.DataFrame(index=df.index)
    ha["HA_Close"] = (df["Open"] + df["High"] + df["Low"] + df["Close"]) / 4.0
    ha_open = [np.nan] * len(df)
    if len(df) > 0:
        ha_open[0] = (df["Open"].iloc[0] + df["Close"].iloc[0]) / 2.0
        for i in range(1, len(df)):
            ha_open[i] = (ha_open[i - 1] + ha["HA_Close"].iloc[i - 1]) / 2.0
    ha["HA_Open"] = ha_open
    ha["HA_High"] = pd.concat([df["High"], ha["HA_Open"], ha["HA_Close"]], axis=1).max(axis=1)
    ha["HA_Low"] = pd.concat([df["Low"], ha["HA_Open"], ha["HA_Close"]], axis=1).min(axis=1)
    ha["HA_Bullish"] = ha["HA_Close"] > ha["HA_Open"]
    return ha


def crossed_above(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a > b) & (a.shift(1) <= b.shift(1))


def crossed_below(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a < b) & (a.shift(1) >= b.shift(1))


# ----------------------------------------------------------------------
# RSI -> projected price targets (ported from the supplied Pine Script v6
# "RSI to Price Projection" indicator)
# ----------------------------------------------------------------------
DEFAULT_RSI_TARGETS = [87.4, 83.75, 76.7, 73.4, 70.0, 60.0, 50.0,
                       43.83, 40.54, 23.67, 30, 20.5, 14, 12]


def rsi_price_targets(df: pd.DataFrame, length: int = 14,
                       targets=None) -> dict:
    """
    Replicates the Pine Script's get_price_for_rsi() logic: given the RMA
    state as of the second-to-last bar, solve for the close price on the
    last bar that would produce each target RSI level.
    Returns {target_rsi: projected_price or None}.
    """
    if targets is None:
        targets = DEFAULT_RSI_TARGETS
    close = df["Close"]
    if len(close) < length + 2:
        return {t: None for t in targets}

    delta = close.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    roll_up = rma(up, length)
    roll_down = rma(down, length)

    # "up"/"down" (RMA incl. last bar) and the last bar's own contribution
    up_last = roll_up.iloc[-1]
    down_last = roll_down.iloc[-1]
    curr_up = max(close.iloc[-1] - close.iloc[-2], 0)
    curr_down = max(close.iloc[-2] - close.iloc[-1], 0)

    if length <= 1 or pd.isna(up_last) or pd.isna(down_last):
        return {t: None for t in targets}

    prev_up = (up_last * length - curr_up) / (length - 1)
    prev_down = (down_last * length - curr_down) / (length - 1)
    close_prev = close.iloc[-2]

    out = {}
    for t in targets:
        if t <= 0 or t >= 100:
            out[t] = None
            continue
        target_rs = t / (100.0 - t)
        d = (target_rs * prev_down - prev_up) * (length - 1)
        if d >= 0:
            out[t] = float(close_prev + d)
        else:
            loss = (prev_up / target_rs - prev_down) * (length - 1) if target_rs != 0 else np.nan
            out[t] = float(close_prev - loss) if loss >= 0 else None
    return out


def rsi_range_for_bar(df: pd.DataFrame, length: int = 14):
    """
    Mirrors the Pine Script's rsi_min/rsi_max: the RSI value that would
    result if the current (last) bar's Low or High were its closing price
    instead - i.e. the range of RSI achievable given the bar already printed.
    Returns (rsi_min, rsi_max), either possibly None.
    """
    close = df["Close"]
    if len(close) < length + 2:
        return None, None

    delta = close.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    roll_up = rma(up, length)
    roll_down = rma(down, length)

    up_last = roll_up.iloc[-1]
    down_last = roll_down.iloc[-1]
    curr_up = max(close.iloc[-1] - close.iloc[-2], 0)
    curr_down = max(close.iloc[-2] - close.iloc[-1], 0)

    if length <= 1 or pd.isna(up_last) or pd.isna(down_last):
        return None, None

    prev_up = (up_last * length - curr_up) / (length - 1)
    prev_down = (down_last * length - curr_down) / (length - 1)
    close_prev = close.iloc[-2]

    def _rsi_for_price(new_close: float):
        change_ = new_close - close_prev
        cu = max(change_, 0)
        cd = max(-change_, 0)
        new_up = (prev_up * (length - 1) + cu) / length
        new_down = (prev_down * (length - 1) + cd) / length
        if new_down > 0:
            return 100 - (100 / (1 + new_up / new_down))
        return 100.0 if new_up > 0 else None

    rsi_min = _rsi_for_price(float(df["Low"].iloc[-1]))
    rsi_max = _rsi_for_price(float(df["High"].iloc[-1]))
    return rsi_min, rsi_max


# ----------------------------------------------------------------------
# Fibonacci retracement / extension levels
# ----------------------------------------------------------------------
FIB_RATIOS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0, 1.272, 1.618, 2.618]


def fibonacci_levels(df: pd.DataFrame, lookback: int = 60) -> dict:
    """
    Finds the swing high/low within the trailing `lookback` bars and computes
    standard Fibonacci retracement (0-100%) and extension (>100%) price
    levels, oriented by which swing point printed more recently.
    Returns {ratio: price}.
    """
    sub = df.iloc[-lookback:] if len(df) > lookback else df
    if len(sub) < 2:
        return {}
    swing_high = float(sub["High"].max())
    swing_low = float(sub["Low"].min())
    diff = swing_high - swing_low
    if diff <= 0:
        return {}

    high_idx = sub["High"].idxmax()
    low_idx = sub["Low"].idxmin()
    uptrend = low_idx < high_idx  # low printed first -> measuring a rally

    levels = {}
    for r in FIB_RATIOS:
        if uptrend:
            price = swing_high - diff * r if r <= 1 else swing_high + diff * (r - 1)
        else:
            price = swing_low + diff * r if r <= 1 else swing_low - diff * (r - 1)
        levels[r] = round(float(price), 2)
    return levels


# ----------------------------------------------------------------------
# Trend line / channel (linear regression) on any series (price or RSI)
# ----------------------------------------------------------------------
def regression_channel(series: pd.Series, lookback: int = 60):
    """
    Fits a linear regression trend line to the last `lookback` points of
    `series` and returns (trend_line, upper_channel, lower_channel) as
    pd.Series aligned to series.index (NaN outside the lookback window).
    """
    s = series.dropna()
    if len(s) < max(5, lookback // 3):
        empty = pd.Series(np.nan, index=series.index)
        return empty, empty.copy(), empty.copy()

    window = s.iloc[-lookback:]
    x = np.arange(len(window))
    y = window.values.astype(float)
    slope, intercept = np.polyfit(x, y, 1)
    trend = slope * x + intercept
    resid = y - trend
    upper = trend + resid.max()
    lower = trend + resid.min()

    trend_s = pd.Series(np.nan, index=series.index)
    upper_s = pd.Series(np.nan, index=series.index)
    lower_s = pd.Series(np.nan, index=series.index)
    trend_s.loc[window.index] = trend
    upper_s.loc[window.index] = upper
    lower_s.loc[window.index] = lower
    return trend_s, upper_s, lower_s


# ----------------------------------------------------------------------
# Support / resistance via swing pivots
# ----------------------------------------------------------------------
def support_resistance(df: pd.DataFrame, order: int = 5, lookback: int = 250,
                        n_levels: int = 2):
    """
    Finds swing-high/swing-low pivots over the trailing `lookback` bars and
    returns the nearest `n_levels` supports (below current close) and
    resistances (above current close).
    """
    sub = df.iloc[-lookback:] if len(df) > lookback else df
    if len(sub) < order * 2 + 1:
        return [], []

    highs = sub["High"].values
    lows = sub["Low"].values
    hi_idx = argrelextrema(highs, np.greater_equal, order=order)[0]
    lo_idx = argrelextrema(lows, np.less_equal, order=order)[0]

    res_levels = sorted(set(np.round(highs[hi_idx], 2).tolist()), reverse=True)
    sup_levels = sorted(set(np.round(lows[lo_idx], 2).tolist()), reverse=True)

    last_close = df["Close"].iloc[-1]
    resistances = sorted([r for r in res_levels if r > last_close])[:n_levels]
    supports = sorted([s for s in sup_levels if s < last_close], reverse=True)[:n_levels]
    return supports, resistances


# ----------------------------------------------------------------------
# Higher-timeframe resample
# ----------------------------------------------------------------------
def resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    agg = {"Open": "first", "High": "max", "Low": "min",
           "Close": "last", "Volume": "sum"}
    out = df.resample(rule).agg(agg)
    return out.dropna(subset=["Close"])


def weekly(df: pd.DataFrame) -> pd.DataFrame:
    return resample_ohlcv(df, "W-FRI")


def monthly(df: pd.DataFrame) -> pd.DataFrame:
    return resample_ohlcv(df, "ME")
