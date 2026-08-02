"""
pattern_engine.py
=================
Detects chart patterns on daily, weekly, and monthly OHLCV DataFrames.

Patterns detected:
  1.  Triangle          (Ascending / Descending / Symmetrical)
  2.  Channel           (Ascending / Descending / Horizontal)
  3.  Wedge             (Rising / Falling)
  4.  Trendline Breakout (Bullish / Bearish)
  5.  Cup & Handle
  6.  Pennant           (Bullish / Bearish)
  7.  Flag              (Bull / Bear)
  8.  Head & Shoulders  (H&S Top / Inverse H&S)
  9.  Double Top / Double Bottom
  10. High Delivery     (gap-up large-body candle — strong momentum)
  11. 52-Week High / 52-Week Low
  12. All-Time High / All-Time Low

Each detected pattern returns a PatternResult with:
  - name        : pattern name
  - timeframe   : "daily" | "weekly" | "monthly"
  - strength    : "Strong" | "Medium" | "Weak"
  - direction   : "Bullish" | "Bearish" | "Neutral"
  - start_date  : when pattern began (for chart lines)
  - end_date    : when pattern completed / confirmed
  - key_levels  : dict of price levels (support, resistance, neckline, etc.)
  - description : human-readable summary
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional
from scipy.signal import argrelextrema


# ── Data class ────────────────────────────────────────────────────────────────
@dataclass
class PatternResult:
    name:        str
    timeframe:   str                          # "daily" | "weekly" | "monthly"
    strength:    str                          # "Strong" | "Medium" | "Weak"
    direction:   str                          # "Bullish" | "Bearish" | "Neutral"
    start_date:  str                          # YYYY-MM-DD
    end_date:    str                          # YYYY-MM-DD
    key_levels:  dict = field(default_factory=dict)
    description: str  = ""

    def to_dict(self) -> dict:
        return {
            "name":        self.name,
            "timeframe":   self.timeframe,
            "strength":    self.strength,
            "direction":   self.direction,
            "start_date":  self.start_date,
            "end_date":    self.end_date,
            "key_levels":  self.key_levels,
            "description": self.description,
        }


def _fmt(ts) -> str:
    return pd.Timestamp(ts).strftime("%Y-%m-%d")


def _pivots(df: pd.DataFrame, order: int = 5):
    """Returns arrays of (idx, price) for swing highs and lows."""
    hi = argrelextrema(df["High"].values, np.greater_equal, order=order)[0]
    lo = argrelextrema(df["Low"].values,  np.less_equal,   order=order)[0]
    return hi, lo


def _slope(x: np.ndarray, y: np.ndarray) -> float:
    if len(x) < 2:
        return 0.0
    m, _ = np.polyfit(x, y, 1)
    return float(m)


def _pct(a, b) -> float:
    """% difference between a and b relative to b."""
    return abs(a - b) / b * 100 if b else 0.0


# ── 1. Triangle ───────────────────────────────────────────────────────────────
def detect_triangle(df: pd.DataFrame, tf: str, lookback: int = 60) -> list[PatternResult]:
    results = []
    sub = df.iloc[-lookback:] if len(df) > lookback else df
    if len(sub) < 20:
        return results

    hi_idx, lo_idx = _pivots(sub, order=max(3, lookback // 20))
    if len(hi_idx) < 2 or len(lo_idx) < 2:
        return results

    hi_prices = sub["High"].values[hi_idx]
    lo_prices = sub["Low"].values[lo_idx]

    hi_slope = _slope(hi_idx.astype(float), hi_prices)
    lo_slope = _slope(lo_idx.astype(float), lo_prices)

    price_range = sub["High"].max() - sub["Low"].min()
    if price_range == 0:
        return results

    # Normalise slope to % of range per bar
    norm_hi = hi_slope / price_range * 10
    norm_lo = lo_slope / price_range * 10

    start_dt = _fmt(sub.index[0])
    end_dt   = _fmt(sub.index[-1])
    close    = float(sub["Close"].iloc[-1])
    resist   = round(float(np.polyval(np.polyfit(hi_idx.astype(float), hi_prices, 1), len(sub) - 1)), 2)
    support  = round(float(np.polyval(np.polyfit(lo_idx.astype(float), lo_prices, 1), len(sub) - 1)), 2)

    # Volume contraction check
    vol_early = sub["Volume"].iloc[:len(sub)//2].mean()
    vol_late  = sub["Volume"].iloc[len(sub)//2:].mean()
    vol_contracted = vol_late < vol_early * 0.85

    # Ascending triangle: flat resistance + rising support
    if abs(norm_hi) < 0.05 and norm_lo > 0.02:
        strength = "Strong" if vol_contracted and close > support else "Medium"
        results.append(PatternResult(
            name="Ascending Triangle", timeframe=tf, strength=strength,
            direction="Bullish", start_date=start_dt, end_date=end_dt,
            key_levels={"resistance": resist, "support": support},
            description=f"Flat resistance ~{resist}, rising support ~{support}. Bullish breakout above {resist}."
        ))
    # Descending triangle: declining resistance + flat support
    elif abs(norm_lo) < 0.05 and norm_hi < -0.02:
        strength = "Strong" if vol_contracted else "Medium"
        results.append(PatternResult(
            name="Descending Triangle", timeframe=tf, strength=strength,
            direction="Bearish", start_date=start_dt, end_date=end_dt,
            key_levels={"resistance": resist, "support": support},
            description=f"Declining resistance ~{resist}, flat support ~{support}. Bearish breakdown below {support}."
        ))
    # Symmetrical triangle: converging trendlines
    elif norm_hi < -0.02 and norm_lo > 0.02:
        strength = "Strong" if vol_contracted else "Medium"
        dir_ = "Bullish" if close > (support + resist) / 2 else "Bearish"
        results.append(PatternResult(
            name="Symmetrical Triangle", timeframe=tf, strength=strength,
            direction=dir_, start_date=start_dt, end_date=end_dt,
            key_levels={"resistance": resist, "support": support},
            description=f"Converging trendlines. Support ~{support}, Resistance ~{resist}. Breakout direction TBD."
        ))
    return results


# ── 2. Channel ────────────────────────────────────────────────────────────────
def detect_channel(df: pd.DataFrame, tf: str, lookback: int = 60) -> list[PatternResult]:
    results = []
    sub = df.iloc[-lookback:] if len(df) > lookback else df
    if len(sub) < 20:
        return results

    hi_idx, lo_idx = _pivots(sub, order=max(3, lookback // 15))
    if len(hi_idx) < 2 or len(lo_idx) < 2:
        return results

    hi_prices = sub["High"].values[hi_idx]
    lo_prices = sub["Low"].values[lo_idx]
    hi_slope  = _slope(hi_idx.astype(float), hi_prices)
    lo_slope  = _slope(lo_idx.astype(float), lo_prices)

    price_range = sub["High"].max() - sub["Low"].min()
    if price_range == 0:
        return results

    norm_hi = hi_slope / price_range * 10
    norm_lo = lo_slope / price_range * 10
    parallel = abs(norm_hi - norm_lo) < 0.04   # roughly parallel

    if not parallel:
        return results

    start_dt  = _fmt(sub.index[0])
    end_dt    = _fmt(sub.index[-1])
    ch_top    = round(float(np.polyval(np.polyfit(hi_idx.astype(float), hi_prices, 1), len(sub)-1)), 2)
    ch_bottom = round(float(np.polyval(np.polyfit(lo_idx.astype(float), lo_prices, 1), len(sub)-1)), 2)
    close     = float(sub["Close"].iloc[-1])

    if norm_hi > 0.03:
        name  = "Ascending Channel"
        dir_  = "Bullish"
        desc  = f"Rising channel. Support ~{ch_bottom}, Resistance ~{ch_top}."
    elif norm_hi < -0.03:
        name  = "Descending Channel"
        dir_  = "Bearish"
        desc  = f"Falling channel. Support ~{ch_bottom}, Resistance ~{ch_top}."
    else:
        name  = "Horizontal Channel"
        dir_  = "Neutral"
        desc  = f"Sideways range. Support ~{ch_bottom}, Resistance ~{ch_top}."

    # Strength: wider channel + more touches = stronger
    n_touches = len(hi_idx) + len(lo_idx)
    strength  = "Strong" if n_touches >= 6 else "Medium" if n_touches >= 4 else "Weak"

    results.append(PatternResult(
        name=name, timeframe=tf, strength=strength, direction=dir_,
        start_date=start_dt, end_date=end_dt,
        key_levels={"channel_top": ch_top, "channel_bottom": ch_bottom},
        description=desc
    ))
    return results


# ── 3. Wedge ──────────────────────────────────────────────────────────────────
def detect_wedge(df: pd.DataFrame, tf: str, lookback: int = 60) -> list[PatternResult]:
    results = []
    sub = df.iloc[-lookback:] if len(df) > lookback else df
    if len(sub) < 20:
        return results

    hi_idx, lo_idx = _pivots(sub, order=max(3, lookback // 15))
    if len(hi_idx) < 2 or len(lo_idx) < 2:
        return results

    hi_prices = sub["High"].values[hi_idx]
    lo_prices = sub["Low"].values[lo_idx]
    hi_slope  = _slope(hi_idx.astype(float), hi_prices)
    lo_slope  = _slope(lo_idx.astype(float), lo_prices)

    price_range = sub["High"].max() - sub["Low"].min()
    if price_range == 0:
        return results

    norm_hi = hi_slope / price_range * 10
    norm_lo = lo_slope / price_range * 10

    # Both lines slope same direction but converging
    both_up   = norm_hi > 0.02 and norm_lo > 0.02 and norm_lo > norm_hi  # rising wedge
    both_down = norm_hi < -0.02 and norm_lo < -0.02 and norm_hi < norm_lo # falling wedge

    if not (both_up or both_down):
        return results

    start_dt = _fmt(sub.index[0])
    end_dt   = _fmt(sub.index[-1])
    ch_top   = round(float(np.polyval(np.polyfit(hi_idx.astype(float), hi_prices, 1), len(sub)-1)), 2)
    ch_bot   = round(float(np.polyval(np.polyfit(lo_idx.astype(float), lo_prices, 1), len(sub)-1)), 2)

    vol_early = sub["Volume"].iloc[:len(sub)//2].mean()
    vol_late  = sub["Volume"].iloc[len(sub)//2:].mean()
    vol_ok    = vol_late < vol_early * 0.9

    if both_up:
        strength = "Strong" if vol_ok else "Medium"
        results.append(PatternResult(
            name="Rising Wedge", timeframe=tf, strength=strength,
            direction="Bearish",
            start_date=start_dt, end_date=end_dt,
            key_levels={"wedge_top": ch_top, "wedge_bottom": ch_bot},
            description=f"Rising wedge — bearish reversal. Support ~{ch_bot}, Resistance ~{ch_top}."
        ))
    else:
        strength = "Strong" if vol_ok else "Medium"
        results.append(PatternResult(
            name="Falling Wedge", timeframe=tf, strength=strength,
            direction="Bullish",
            start_date=start_dt, end_date=end_dt,
            key_levels={"wedge_top": ch_top, "wedge_bottom": ch_bot},
            description=f"Falling wedge — bullish reversal. Support ~{ch_bot}, Resistance ~{ch_top}."
        ))
    return results


# ── 4. Trendline Breakout ─────────────────────────────────────────────────────
def detect_trendline_breakout(df: pd.DataFrame, tf: str, lookback: int = 60) -> list[PatternResult]:
    results = []
    sub = df.iloc[-lookback:] if len(df) > lookback else df
    if len(sub) < 15:
        return results

    hi_idx, lo_idx = _pivots(sub, order=max(3, lookback // 15))
    close = sub["Close"].iloc[-1]
    vol_avg = sub["Volume"].rolling(20).mean().iloc[-1]
    vol_now = sub["Volume"].iloc[-1]
    vol_surge = float(vol_now) > float(vol_avg) * 1.5 if pd.notna(vol_avg) else False

    # Downtrend trendline from swing highs → bullish breakout if price above
    if len(hi_idx) >= 2:
        hi_prices = sub["High"].values[hi_idx]
        m, b = np.polyfit(hi_idx.astype(float), hi_prices, 1)
        tl_now = m * (len(sub) - 1) + b
        if float(close) > tl_now * 1.005:   # closed 0.5% above trendline
            strength = "Strong" if vol_surge else "Medium"
            results.append(PatternResult(
                name="Trendline Breakout", timeframe=tf,
                strength=strength, direction="Bullish",
                start_date=_fmt(sub.index[hi_idx[0]]),
                end_date=_fmt(sub.index[-1]),
                key_levels={"trendline": round(tl_now, 2), "close": round(float(close), 2)},
                description=f"Price broke above descending trendline ~{round(tl_now,2)} with {'strong' if vol_surge else 'normal'} volume."
            ))

    # Uptrend trendline from swing lows → bearish breakdown if price below
    if len(lo_idx) >= 2:
        lo_prices = sub["Low"].values[lo_idx]
        m, b = np.polyfit(lo_idx.astype(float), lo_prices, 1)
        tl_now = m * (len(sub) - 1) + b
        if float(close) < tl_now * 0.995:
            strength = "Strong" if vol_surge else "Medium"
            results.append(PatternResult(
                name="Trendline Breakdown", timeframe=tf,
                strength=strength, direction="Bearish",
                start_date=_fmt(sub.index[lo_idx[0]]),
                end_date=_fmt(sub.index[-1]),
                key_levels={"trendline": round(tl_now, 2), "close": round(float(close), 2)},
                description=f"Price broke below ascending trendline ~{round(tl_now,2)} with {'strong' if vol_surge else 'normal'} volume."
            ))
    return results


# ── 5. Cup & Handle ───────────────────────────────────────────────────────────
def detect_cup_handle(df: pd.DataFrame, tf: str, lookback: int = 120) -> list[PatternResult]:
    results = []
    sub = df.iloc[-lookback:] if len(df) > lookback else df
    if len(sub) < 40:
        return results

    n   = len(sub)
    closes = sub["Close"].values

    # Cup: left rim high, bottom, right rim high (roughly equal), then handle
    left_third  = closes[:n//3]
    mid_third   = closes[n//3:2*n//3]
    right_third = closes[2*n//3:]

    left_high   = float(np.max(left_third))
    cup_low     = float(np.min(mid_third))
    right_high  = float(np.max(right_third[-n//6:]))   # recent right rim

    # Cup shape checks
    depth_pct  = (left_high - cup_low) / left_high * 100
    rim_sym    = _pct(left_high, right_high)            # rims roughly equal
    handle_low = float(np.min(right_third))

    if not (15 < depth_pct < 60 and rim_sym < 10 and right_high > cup_low * 1.1):
        return results

    # Handle: shallow pullback from right rim
    handle_retrace = (right_high - handle_low) / (right_high - cup_low) * 100
    if not (10 < handle_retrace < 50):
        return results

    close_now = float(sub["Close"].iloc[-1])
    breakout  = close_now >= right_high * 0.99

    strength = "Strong" if (breakout and depth_pct > 25 and rim_sym < 5) else \
               "Medium" if breakout else "Weak"

    results.append(PatternResult(
        name="Cup & Handle", timeframe=tf,
        strength=strength, direction="Bullish",
        start_date=_fmt(sub.index[0]), end_date=_fmt(sub.index[-1]),
        key_levels={"cup_low": round(cup_low, 2), "rim": round(right_high, 2),
                    "handle_low": round(handle_low, 2)},
        description=f"Cup depth {depth_pct:.1f}%, Handle retrace {handle_retrace:.1f}%. "
                    f"{'Breaking out above' if breakout else 'Approaching'} rim ~{round(right_high,2)}."
    ))
    return results


# ── 6. Pennant ────────────────────────────────────────────────────────────────
def detect_pennant(df: pd.DataFrame, tf: str, lookback: int = 40) -> list[PatternResult]:
    results = []
    sub = df.iloc[-lookback:] if len(df) > lookback else df
    if len(sub) < 15:
        return results

    closes = sub["Close"].values
    # Flagpole: sharp move in first third
    pole_bars  = max(5, len(sub) // 4)
    pole_move  = (closes[pole_bars] - closes[0]) / closes[0] * 100
    consolidation = sub.iloc[pole_bars:]

    if abs(pole_move) < 5:      # need at least 5% pole
        return results

    if len(consolidation) < 8:
        return results

    # Consolidation: converging high/low (pennant shape)
    hi_idx, lo_idx = _pivots(consolidation, order=2)
    if len(hi_idx) < 2 or len(lo_idx) < 2:
        return results

    hi_prices = consolidation["High"].values[hi_idx]
    lo_prices = consolidation["Low"].values[lo_idx]
    hi_slope  = _slope(hi_idx.astype(float), hi_prices)
    lo_slope  = _slope(lo_idx.astype(float), lo_prices)

    converging = hi_slope < 0 and lo_slope > 0
    if not converging:
        return results

    dir_   = "Bullish" if pole_move > 0 else "Bearish"
    target = round(float(closes[-1]) + (closes[pole_bars] - closes[0]), 2)
    strength = "Strong" if abs(pole_move) > 10 else "Medium"

    results.append(PatternResult(
        name=f"{'Bullish' if dir_=='Bullish' else 'Bearish'} Pennant",
        timeframe=tf, strength=strength, direction=dir_,
        start_date=_fmt(sub.index[0]), end_date=_fmt(sub.index[-1]),
        key_levels={"pole_move_pct": round(pole_move, 1), "target": target},
        description=f"Pennant after {pole_move:+.1f}% pole move. Projected target ~{target}."
    ))
    return results


# ── 7. Flag ───────────────────────────────────────────────────────────────────
def detect_flag(df: pd.DataFrame, tf: str, lookback: int = 40) -> list[PatternResult]:
    results = []
    sub = df.iloc[-lookback:] if len(df) > lookback else df
    if len(sub) < 15:
        return results

    closes = sub["Close"].values
    pole_bars = max(5, len(sub) // 4)
    pole_move = (closes[pole_bars] - closes[0]) / closes[0] * 100

    if abs(pole_move) < 5:
        return results

    flag = sub.iloc[pole_bars:]
    if len(flag) < 5:
        return results

    # Flag: parallel channel sloping AGAINST the pole
    hi_idx, lo_idx = _pivots(flag, order=2)
    if len(hi_idx) < 2 or len(lo_idx) < 2:
        return results

    hi_prices = flag["High"].values[hi_idx]
    lo_prices = flag["Low"].values[lo_idx]
    hi_slope  = _slope(hi_idx.astype(float), hi_prices)
    lo_slope  = _slope(lo_idx.astype(float), lo_prices)

    # Parallel (same direction slope)
    parallel  = abs(hi_slope - lo_slope) / (abs(hi_slope) + abs(lo_slope) + 1e-9) < 0.5
    counter   = (pole_move > 0 and hi_slope < 0) or (pole_move < 0 and hi_slope > 0)

    if not (parallel and counter):
        return results

    dir_   = "Bullish" if pole_move > 0 else "Bearish"
    target = round(float(closes[-1]) + (closes[pole_bars] - closes[0]), 2)
    strength = "Strong" if abs(pole_move) > 10 else "Medium"

    results.append(PatternResult(
        name=f"{'Bull' if dir_=='Bullish' else 'Bear'} Flag",
        timeframe=tf, strength=strength, direction=dir_,
        start_date=_fmt(sub.index[0]), end_date=_fmt(sub.index[-1]),
        key_levels={"pole_move_pct": round(pole_move, 1), "target": target},
        description=f"Flag after {pole_move:+.1f}% flagpole. Projected target ~{target}."
    ))
    return results


# ── 8. Head & Shoulders ───────────────────────────────────────────────────────
def detect_head_shoulders(df: pd.DataFrame, tf: str, lookback: int = 120) -> list[PatternResult]:
    results = []
    sub = df.iloc[-lookback:] if len(df) > lookback else df
    if len(sub) < 30:
        return results

    hi_idx, lo_idx = _pivots(sub, order=max(4, lookback // 20))

    # Need at least 3 swing highs for H&S top
    if len(hi_idx) >= 3:
        for i in range(len(hi_idx) - 2):
            lsh = sub["High"].values[hi_idx[i]]
            head = sub["High"].values[hi_idx[i+1]]
            rsh  = sub["High"].values[hi_idx[i+2]]

            # Head must be highest; shoulders roughly equal
            if head <= lsh or head <= rsh:
                continue
            if _pct(lsh, rsh) > 8:   # shoulders within 8%
                continue

            # Neckline from lows between shoulders
            between = lo_idx[(lo_idx > hi_idx[i]) & (lo_idx < hi_idx[i+2])]
            if len(between) < 1:
                continue
            neckline = float(sub["Low"].values[between].mean())
            close    = float(sub["Close"].iloc[-1])
            broken   = close < neckline * 0.99

            strength = "Strong" if (broken and _pct(lsh, rsh) < 3) else \
                       "Medium" if broken else "Weak"
            results.append(PatternResult(
                name="Head & Shoulders Top", timeframe=tf,
                strength=strength, direction="Bearish",
                start_date=_fmt(sub.index[hi_idx[i]]),
                end_date=_fmt(sub.index[-1]),
                key_levels={"head": round(head, 2), "left_shoulder": round(lsh, 2),
                            "right_shoulder": round(rsh, 2), "neckline": round(neckline, 2)},
                description=f"H&S Top. Head {round(head,2)}, neckline ~{round(neckline,2)}. "
                            f"{'Neckline broken — bearish' if broken else 'Watching neckline'}."
            ))
            break   # report first valid pattern

    # Inverse H&S from swing lows
    if len(lo_idx) >= 3:
        for i in range(len(lo_idx) - 2):
            lsh  = sub["Low"].values[lo_idx[i]]
            head = sub["Low"].values[lo_idx[i+1]]
            rsh  = sub["Low"].values[lo_idx[i+2]]

            if head >= lsh or head >= rsh:
                continue
            if _pct(lsh, rsh) > 8:
                continue

            between  = hi_idx[(hi_idx > lo_idx[i]) & (hi_idx < lo_idx[i+2])]
            if len(between) < 1:
                continue
            neckline = float(sub["High"].values[between].mean())
            close    = float(sub["Close"].iloc[-1])
            broken   = close > neckline * 1.01

            strength = "Strong" if (broken and _pct(lsh, rsh) < 3) else \
                       "Medium" if broken else "Weak"
            results.append(PatternResult(
                name="Inverse Head & Shoulders", timeframe=tf,
                strength=strength, direction="Bullish",
                start_date=_fmt(sub.index[lo_idx[i]]),
                end_date=_fmt(sub.index[-1]),
                key_levels={"head": round(head, 2), "left_shoulder": round(lsh, 2),
                            "right_shoulder": round(rsh, 2), "neckline": round(neckline, 2)},
                description=f"Inverse H&S. Head low {round(head,2)}, neckline ~{round(neckline,2)}. "
                            f"{'Neckline broken — bullish' if broken else 'Watching neckline'}."
            ))
            break
    return results


# ── 9. Double Top / Double Bottom ─────────────────────────────────────────────
def detect_double_top_bottom(df: pd.DataFrame, tf: str, lookback: int = 80) -> list[PatternResult]:
    results = []
    sub = df.iloc[-lookback:] if len(df) > lookback else df
    if len(sub) < 20:
        return results

    hi_idx, lo_idx = _pivots(sub, order=max(4, lookback // 15))
    close = float(sub["Close"].iloc[-1])

    # Double Top: two swing highs at similar level
    if len(hi_idx) >= 2:
        for i in range(len(hi_idx) - 1):
            h1 = sub["High"].values[hi_idx[i]]
            h2 = sub["High"].values[hi_idx[i+1]]
            if _pct(h1, h2) > 3:    # within 3%
                continue
            # Valley between the two tops
            valley_slice = lo_idx[(lo_idx > hi_idx[i]) & (lo_idx < hi_idx[i+1])]
            if len(valley_slice) < 1:
                continue
            neckline = float(sub["Low"].values[valley_slice].min())
            broken   = close < neckline * 0.99
            top_avg  = (h1 + h2) / 2
            target   = round(neckline - (top_avg - neckline), 2)

            strength = "Strong" if broken and _pct(h1, h2) < 1.5 else \
                       "Medium" if broken else "Weak"
            results.append(PatternResult(
                name="Double Top", timeframe=tf,
                strength=strength, direction="Bearish",
                start_date=_fmt(sub.index[hi_idx[i]]),
                end_date=_fmt(sub.index[-1]),
                key_levels={"top1": round(h1,2), "top2": round(h2,2),
                            "neckline": round(neckline,2), "target": target},
                description=f"Double Top at ~{round(top_avg,2)}, neckline {round(neckline,2)}. "
                            f"{'Broken — target' if broken else 'Watch neckline'} ~{target}."
            ))
            break

    # Double Bottom: two swing lows at similar level
    if len(lo_idx) >= 2:
        for i in range(len(lo_idx) - 1):
            l1 = sub["Low"].values[lo_idx[i]]
            l2 = sub["Low"].values[lo_idx[i+1]]
            if _pct(l1, l2) > 3:
                continue
            peak_slice = hi_idx[(hi_idx > lo_idx[i]) & (hi_idx < lo_idx[i+1])]
            if len(peak_slice) < 1:
                continue
            neckline = float(sub["High"].values[peak_slice].max())
            broken   = close > neckline * 1.01
            bot_avg  = (l1 + l2) / 2
            target   = round(neckline + (neckline - bot_avg), 2)

            strength = "Strong" if broken and _pct(l1, l2) < 1.5 else \
                       "Medium" if broken else "Weak"
            results.append(PatternResult(
                name="Double Bottom", timeframe=tf,
                strength=strength, direction="Bullish",
                start_date=_fmt(sub.index[lo_idx[i]]),
                end_date=_fmt(sub.index[-1]),
                key_levels={"bottom1": round(l1,2), "bottom2": round(l2,2),
                            "neckline": round(neckline,2), "target": target},
                description=f"Double Bottom at ~{round(bot_avg,2)}, neckline {round(neckline,2)}. "
                            f"{'Broken — target' if broken else 'Watch neckline'} ~{target}."
            ))
            break
    return results


# ── 10. High Delivery ─────────────────────────────────────────────────────────
def detect_high_delivery(df: pd.DataFrame, tf: str, lookback: int = 5) -> list[PatternResult]:
    """Large-body candle with gap-up or significantly above average range."""
    results = []
    if len(df) < 20:
        return results

    last   = df.iloc[-1]
    body   = abs(float(last["Close"]) - float(last["Open"]))
    range_ = float(last["High"]) - float(last["Low"])
    avg_range = (df["High"] - df["Low"]).rolling(20).mean().iloc[-1]

    if pd.isna(avg_range) or avg_range == 0:
        return results

    body_pct  = body / float(last["Close"]) * 100
    range_pct = range_ / float(avg_range)
    gap_up    = float(last["Open"]) > float(df["Close"].iloc[-2]) * 1.02

    bullish = float(last["Close"]) > float(last["Open"])
    if not bullish:
        return results

    if range_pct > 2.5 and body_pct > 2:
        strength = "Strong"
    elif range_pct > 1.8 and body_pct > 1:
        strength = "Medium"
    elif range_pct > 1.4 or gap_up:
        strength = "Weak"
    else:
        return results

    desc_parts = []
    if gap_up:
        desc_parts.append("Gap-up open")
    desc_parts.append(f"Body {body_pct:.1f}% of price")
    desc_parts.append(f"Range {range_pct:.1f}x avg")

    results.append(PatternResult(
        name="High Delivery" + (" (Gap-Up)" if gap_up else ""),
        timeframe=tf, strength=strength, direction="Bullish",
        start_date=_fmt(df.index[-1]), end_date=_fmt(df.index[-1]),
        key_levels={"close": round(float(last["Close"]), 2),
                    "open": round(float(last["Open"]), 2),
                    "range_vs_avg": round(range_pct, 2)},
        description=". ".join(desc_parts) + "."
    ))
    return results


# ── 11. 52-Week High / Low ────────────────────────────────────────────────────
def detect_52week_extremes(df: pd.DataFrame, tf: str) -> list[PatternResult]:
    results = []
    if len(df) < 50:
        return results

    last_date = df.index[-1]
    year_ago  = last_date - pd.DateOffset(weeks=52)
    sub_52    = df[df.index >= year_ago]
    if sub_52.empty:
        return results

    close     = float(df["Close"].iloc[-1])
    high_52   = float(sub_52["High"].max())
    low_52    = float(sub_52["Low"].min())
    pct_from_high = (close - high_52) / high_52 * 100
    pct_from_low  = (close - low_52)  / low_52  * 100

    # Near/at 52W high
    if pct_from_high >= -2:
        strength = "Strong" if pct_from_high >= -0.5 else "Medium"
        results.append(PatternResult(
            name="52-Week High", timeframe=tf,
            strength=strength, direction="Bullish",
            start_date=_fmt(year_ago), end_date=_fmt(last_date),
            key_levels={"52w_high": round(high_52, 2), "close": round(close, 2),
                        "pct_from_high": round(pct_from_high, 2)},
            description=f"{'At' if pct_from_high >= -0.5 else 'Near'} 52-week high of {round(high_52,2)} ({pct_from_high:+.1f}%)."
        ))

    # Near/at 52W low
    if pct_from_low <= 5:
        strength = "Strong" if pct_from_low <= 1 else "Medium"
        results.append(PatternResult(
            name="52-Week Low", timeframe=tf,
            strength=strength, direction="Bearish",
            start_date=_fmt(year_ago), end_date=_fmt(last_date),
            key_levels={"52w_low": round(low_52, 2), "close": round(close, 2),
                        "pct_from_low": round(pct_from_low, 2)},
            description=f"{'At' if pct_from_low <= 1 else 'Near'} 52-week low of {round(low_52,2)} (+{pct_from_low:.1f}%)."
        ))
    return results


# ── 12. All-Time High / Low ───────────────────────────────────────────────────
def detect_ath_atl(df: pd.DataFrame, tf: str) -> list[PatternResult]:
    results = []
    if len(df) < 50:
        return results

    close   = float(df["Close"].iloc[-1])
    ath     = float(df["High"].max())
    atl     = float(df["Low"].min())
    pct_ath = (close - ath) / ath * 100
    pct_atl = (close - atl) / atl * 100

    if pct_ath >= -2:
        strength = "Strong" if pct_ath >= -0.5 else "Medium"
        results.append(PatternResult(
            name="All-Time High", timeframe=tf,
            strength=strength, direction="Bullish",
            start_date=_fmt(df.index[0]), end_date=_fmt(df.index[-1]),
            key_levels={"ath": round(ath, 2), "close": round(close, 2),
                        "pct_from_ath": round(pct_ath, 2)},
            description=f"{'At' if pct_ath >= -0.5 else 'Near'} all-time high of {round(ath,2)} ({pct_ath:+.1f}%)."
        ))

    if pct_atl <= 5:
        strength = "Strong" if pct_atl <= 1 else "Medium"
        results.append(PatternResult(
            name="All-Time Low", timeframe=tf,
            strength=strength, direction="Bearish",
            start_date=_fmt(df.index[0]), end_date=_fmt(df.index[-1]),
            key_levels={"atl": round(atl, 2), "close": round(close, 2),
                        "pct_from_atl": round(pct_atl, 2)},
            description=f"{'At' if pct_atl <= 1 else 'Near'} all-time low of {round(atl,2)} (+{pct_atl:.1f}%)."
        ))
    return results


# ── Master scanner ────────────────────────────────────────────────────────────
_LOOKBACKS = {"daily": 60, "weekly": 52, "monthly": 36}

def scan_all_patterns(daily: pd.DataFrame,
                      weekly: pd.DataFrame,
                      monthly: pd.DataFrame) -> list[PatternResult]:
    """
    Runs all pattern detectors on daily, weekly, monthly frames.
    Returns a flat list of PatternResult, deduplicated by (name, timeframe).
    """
    all_results: list[PatternResult] = []
    frames = [("daily", daily), ("weekly", weekly), ("monthly", monthly)]

    detectors = [
        detect_triangle,
        detect_channel,
        detect_wedge,
        detect_trendline_breakout,
        detect_pennant,
        detect_flag,
        detect_head_shoulders,
        detect_double_top_bottom,
        detect_high_delivery,
        detect_52week_extremes,
        detect_ath_atl,
    ]
    cup_lookbacks = {"daily": 120, "weekly": 104, "monthly": 60}

    for tf, df in frames:
        if df is None or len(df) < 15:
            continue
        lb = _LOOKBACKS.get(tf, 60)
        for det in detectors:
            try:
                all_results.extend(det(df, tf, lb) if det not in
                                   (detect_52week_extremes, detect_ath_atl,
                                    detect_high_delivery)
                                   else det(df, tf))
            except Exception:
                pass
        # Cup & Handle with longer lookback
        try:
            all_results.extend(detect_cup_handle(df, tf, cup_lookbacks[tf]))
        except Exception:
            pass

    # Deduplicate: keep highest-strength per (name, timeframe)
    strength_rank = {"Strong": 0, "Medium": 1, "Weak": 2}
    best: dict[tuple, PatternResult] = {}
    for p in all_results:
        key = (p.name, p.timeframe)
        if key not in best or strength_rank[p.strength] < strength_rank[best[key].strength]:
            best[key] = p

    # Sort: Strong first, then by timeframe, then name
    tf_rank = {"daily": 0, "weekly": 1, "monthly": 2}
    return sorted(best.values(),
                  key=lambda p: (strength_rank[p.strength], tf_rank.get(p.timeframe, 3), p.name))


def patterns_summary(patterns: list[PatternResult]) -> dict:
    """
    Returns a compact summary dict for the screener report row:
      {
        "count": int,
        "strongest": str,     e.g. "Double Bottom (D)"
        "bullish":  int,
        "bearish":  int,
        "by_tf": {"daily": [...], "weekly": [...], "monthly": [...]},
        "all": [...]           list of dicts
      }
    """
    if not patterns:
        return {"count": 0, "strongest": "", "bullish": 0, "bearish": 0,
                "by_tf": {"daily": [], "weekly": [], "monthly": []}, "all": []}

    TF_ABBR = {"daily": "D", "weekly": "W", "monthly": "M"}
    strongest = patterns[0]   # already sorted by strength

    by_tf: dict[str, list] = {"daily": [], "weekly": [], "monthly": []}
    for p in patterns:
        by_tf[p.timeframe].append(p.to_dict())

    return {
        "count":    len(patterns),
        "strongest": f"{strongest.name} ({TF_ABBR[strongest.timeframe]}) [{strongest.strength}]",
        "bullish":  sum(1 for p in patterns if p.direction == "Bullish"),
        "bearish":  sum(1 for p in patterns if p.direction == "Bearish"),
        "by_tf":    by_tf,
        "all":      [p.to_dict() for p in patterns],
    }
